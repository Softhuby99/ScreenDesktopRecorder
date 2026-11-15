"""
calibration.py
==============
"Ton kalibrieren" - misst den Ton-Bild-Versatz eines Mikrofons (GUI-frei).

Ablauf (siehe plan-kalibrierung.md, Rev. 2):
  * Aufgenommen wird mit DENSELBEN Einstellungen wie eine echte Aufnahme
    (Modus, Bereich, Bildrate, Encoder, Preset, Verstärkung, Rauschfilter).
    Das Blinkfeld der GUI liegt innerhalb dieses Bereichs; ausgewertet wird
    nur sein Ausschnitt (crop).
  * CALIB_RUNS Durchläufe, jeder mit neuem FFmpeg-Start. Durchlauf 1 misst
    ohne Versatz, die weiteren mit dem ermittelten Versatz (Gegenprobe).
    Schwankt die echte Verspätung zwischen den Durchläufen stärker als
    STABLE_SPREAD_MS, ist eine feste Korrektur auf diesem Gerät nicht
    zuverlässig -> Ergebnis "unstable", die GUI empfiehlt den Sync-Marker.
  * Für jeden Piepton meldet der Audio-Callback, wann er tatsächlich aus
    dem Lautsprecher kommt (outputBufferDacTime); genau dann blitzt das
    Feld. So verfälscht die Ausgabelatenz die Messung nicht.
  * Auswertung nur mit FFmpeg + reinem Python (kein NumPy).
  * Jede Kalibrierung schreibt ein Protokoll. Die Messaufnahmen bleiben nur
    bei Fehlschlag oder Schwankung erhalten (für die Fehlersuche).

Die Bausteine (Signal, Auswertung, BeepPlayer) nutzt auch syncfix.py.
"""

import array
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime

from config import AUDIO_OFFSET_MAX_MS, AUDIO_OFFSET_MIN_MS
from ffmpeg_utils import build_record_command, get_ffmpeg_path
from platform_utils import get_subprocess_flags

# ---------------------------------------------------------------------------
# Parameter
# ---------------------------------------------------------------------------
CALIB_RUNS = 3
CALIB_BEEPS = 5                 # Pieptöne je Durchlauf
CALIB_INTERVAL_S = 2.4          # Abstand (Paarung bis ±1,15 s eindeutig)
CALIB_BEEP_S = 0.12             # Länge eines Pieptons
CALIB_LEAD_S = 3.0              # FFmpeg-Anlauf, bevor der erste Ton kommt
CALIB_TAIL_S = 1.2              # Nachlauf nach dem letzten Ton
CALIB_FLASH_S = 0.15            # Dauer eines Blitzes (GUI)
CALIB_SAMPLE_RATE = 48000
CALIB_FREQ_HZ = 1000.0
SIGNAL_PAD_S = 0.3              # Stille vor dem ersten / nach dem letzten Ton

MIN_PAIRS = 4                   # je Durchlauf mindestens so viele Paare
MAX_SPREAD_MS = 70              # Streuung innerhalb eines Durchlaufs
STABLE_SPREAD_MS = 60           # Schwankung zwischen den Durchläufen
GOAL_REST_MS = 40               # Ziel für den Restversatz
PAIR_WINDOW_S = 1.15            # Ton muss so nah am Blitz liegen

ANALYSIS_RATE = 8000            # Ton-Auswertung: Mono, 8 kHz
ENVELOPE_BLOCK_S = 0.01         # Hüllkurve in 10-ms-Blöcken
BEEP_THRESHOLD = 0.25           # Anteil zwischen Rauschen und lautem Pegel

SOURCE_CALIBRATED = "kalibriert"
SOURCE_MANUAL = "manuell"


# ---------------------------------------------------------------------------
# Signal erzeugen
# ---------------------------------------------------------------------------
def beep_start_samples(rate: int = CALIB_SAMPLE_RATE, beeps: int = CALIB_BEEPS,
                       interval: float = CALIB_INTERVAL_S) -> list[int]:
    """Startsample jedes Pieptons im erzeugten Tonsignal."""
    return [int(round((SIGNAL_PAD_S + k * interval) * rate)) for k in range(beeps)]


def make_beep_pcm(rate: int = CALIB_SAMPLE_RATE, amplitude: float = 0.5,
                  beeps: int = CALIB_BEEPS, interval: float = CALIB_INTERVAL_S) -> bytes:
    """
    Mono-PCM (int16, little endian) mit Pieptönen und je 5 ms
    Ein-/Ausblendung gegen Knacken. Ohne NumPy (array-Modul).
    """
    starts = beep_start_samples(rate, beeps, interval)
    beep_len = int(CALIB_BEEP_S * rate)
    total = starts[-1] + beep_len + int(SIGNAL_PAD_S * rate)
    pcm = array.array("h", bytes(2 * total))
    fade = max(1, int(0.005 * rate))
    peak = int(32767 * amplitude)
    w = 2.0 * math.pi * CALIB_FREQ_HZ / rate
    for start in starts:
        for i in range(beep_len):
            env = min(1.0, i / fade, (beep_len - 1 - i) / fade)
            pcm[start + i] = int(peak * env * math.sin(w * i))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


def signal_seconds(beeps: int = CALIB_BEEPS, interval: float = CALIB_INTERVAL_S) -> float:
    return 2 * SIGNAL_PAD_S + (beeps - 1) * interval + CALIB_BEEP_S


# ---------------------------------------------------------------------------
# Auswertung (reine Funktionen)
# ---------------------------------------------------------------------------
_SHOWINFO_RE = re.compile(r"pts_time:\s*([-\d.]+).*?mean:\[\s*([\d.]+)")
_KEYFRAME_RE = re.compile(r"pts_time:\s*([-\d.]+).*?iskey:1")


def parse_showinfo(text: str) -> list[tuple[float, float]]:
    """(Zeit, mittlere Helligkeit) je Bild aus FFmpeg-showinfo-Zeilen."""
    frames = []
    for line in text.splitlines():
        m = _SHOWINFO_RE.search(line)
        if m:
            try:
                frames.append((float(m.group(1)), float(m.group(2))))
            except ValueError:
                pass
    return frames


def parse_keyframes(text: str) -> list[float]:
    """Zeitpunkte der Schlüsselbilder (iskey:1) aus showinfo-Zeilen."""
    out = []
    for line in text.splitlines():
        m = _KEYFRAME_RE.search(line)
        if m:
            try:
                out.append(float(m.group(1)))
            except ValueError:
                pass
    return out


def flash_onsets(frames: list[tuple[float, float]], min_contrast: float = 60.0,
                 min_gap_s: float = 1.0) -> list[float]:
    """
    Zeitpunkte, an denen das Blinkfeld hell wird. Schwelle in der Mitte
    zwischen dunkelstem und hellstem Bild; ohne ausreichenden Kontrast
    (kein Blitz im Bild) leere Liste.
    """
    if len(frames) < 2:
        return []
    values = [v for _, v in frames]
    lo, hi = min(values), max(values)
    if hi - lo < min_contrast:
        return []
    thr = (lo + hi) / 2.0
    onsets, last = [], -1e9
    for (_t0, v0), (t1, v1) in zip(frames, frames[1:]):
        if v0 < thr <= v1 and t1 - last >= min_gap_s:
            onsets.append(t1)
            last = t1
    return onsets


def envelope(samples, rate: int = ANALYSIS_RATE,
             block_s: float = ENVELOPE_BLOCK_S) -> list[float]:
    """Mittlerer Betrag je Block (Hüllkurve) - reines Python."""
    n = max(1, int(rate * block_s))
    out = []
    for i in range(0, len(samples) - n + 1, n):
        block = samples[i:i + n]
        out.append(sum(abs(s) for s in block) / n)
    return out


def beep_onsets(env: list[float], block_s: float = ENVELOPE_BLOCK_S,
                min_gap_s: float = 1.0, min_snr: float = 4.0) -> list[float]:
    """
    Anfänge der Pieptöne in der (bandpass-gefilterten) Hüllkurve.
    Schwelle: ein Viertel des Abstands zwischen Grundrauschen (Median) und
    lautem Pegel (99,5-%-Wert). Niedrig, weil viele Mikrofone nach dem
    Einsatz erst hochregeln (AGC/Hall) - bei 50 % lag der erkannte Anfang
    in echten Aufnahmen bis zu 70 ms zu spät. Ist der Ton nicht deutlich
    lauter als das Rauschen, wird nichts gemeldet.
    """
    if len(env) < 10:
        return []
    ordered = sorted(env)
    noise = ordered[len(ordered) // 2]
    loud = ordered[min(len(ordered) - 1, int(len(ordered) * 0.995))]
    if loud < max(noise, 1.0) * min_snr:
        return []
    thr = noise + (loud - noise) * BEEP_THRESHOLD
    onsets, last = [], -1e9
    for i in range(1, len(env)):
        t = i * block_s
        if env[i - 1] < thr <= env[i] and t - last >= min_gap_s:
            onsets.append(t)
            last = t
    return onsets


def pair_offsets(video: list[float], audio: list[float],
                 window_s: float = PAIR_WINDOW_S) -> list[float]:
    """Je Blitz der nächstgelegene Ton (Ton - Bild, Sekunden), höchstens window_s."""
    result = []
    for v in video:
        if not audio:
            break
        a = min(audio, key=lambda t: abs(t - v))
        if abs(a - v) <= window_s:
            result.append(a - v)
    return result


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def round10(ms: float) -> int:
    return int(round(ms / 10.0)) * 10


def evaluate(video: list[float], audio: list[float], min_pairs: int = MIN_PAIRS,
             expected: int = CALIB_BEEPS) -> dict:
    """
    Ergebnis EINER Messung (ein Durchlauf bzw. ein Marker).

    Rückgabe: {"ok", "reason", "lateness_ms", "correction_ms", "pairs",
               "spread_ms", "flashes", "beeps", "flash_times", "beep_times"}
      lateness_ms   > 0: Ton kommt in der Datei zu spät
      correction_ms Korrektur relativ zur Datei (= -lateness, 10-ms-Raster)
    """
    res = {"ok": False, "reason": "", "lateness_ms": None, "correction_ms": None,
           "pairs": 0, "spread_ms": None, "flashes": len(video), "beeps": len(audio),
           "flash_times": [round(t, 3) for t in video],
           "beep_times": [round(t, 3) for t in audio]}
    if not audio:
        res["reason"] = ("Kein Piepton erkannt. Bitte Lautsprecher einschalten, "
                         "Lautstärke erhöhen und keinen geschlossenen Kopfhörer verwenden.")
        return res
    if not video:
        res["reason"] = ("Blinken im Bild nicht erkannt. Bitte das Blinkfeld "
                         "während der Messung nicht verdecken.")
        return res
    offsets = pair_offsets(video, audio)
    res["pairs"] = len(offsets)
    if len(offsets) < min_pairs:
        res["reason"] = (f"Nur {len(offsets)} von {expected} Messpunkten erkannt "
                         f"(mindestens {min_pairs} nötig). Bitte in ruhiger Umgebung wiederholen.")
        return res
    ms = [o * 1000.0 for o in offsets]
    spread = max(ms) - min(ms)
    lateness = median(ms)
    res["spread_ms"] = int(round(spread))
    res["lateness_ms"] = int(round(lateness))
    if spread > MAX_SPREAD_MS:
        res["reason"] = (f"Messung zu unruhig (Streuung {spread:.0f} ms). "
                         "Bitte in ruhiger Umgebung wiederholen.")
        return res
    res["correction_ms"] = -round10(lateness)
    res["ok"] = True
    return res


def combine_runs(runs: list[dict]) -> dict:
    """
    Fasst die Durchläufe zusammen. Jeder Eintrag: evaluate()-Ergebnis plus
    "applied_ms" (bei der Aufnahme schon angewendeter Versatz).

    Echte Verspätung je Durchlauf = gemessene Verspätung - angewendeter
    Versatz (der Versatz verschiebt den Ton um applied_ms).

    Rückgabe: {"ok", "stable", "reason", "correction_ms", "true_ms" (Liste),
               "run_spread_ms", "rest_ms" (Rest je Durchlauf, wenn die
               endgültige Korrektur angewendet worden wäre), "good_runs", "runs"}
    """
    good = [r for r in runs if r.get("ok")]
    res = {"ok": False, "stable": False, "reason": "", "correction_ms": None,
           "true_ms": [], "run_spread_ms": None, "rest_ms": [], "good_runs": len(good),
           "runs": len(runs)}
    if len(good) < 2:
        reasons = [r.get("reason") for r in runs if r.get("reason")]
        res["reason"] = reasons[0] if reasons else "Zu wenige gültige Durchläufe."
        return res
    true_ms = [r["lateness_ms"] - int(r.get("applied_ms", 0)) for r in good]
    correction = -round10(median(true_ms))
    res["true_ms"] = true_ms
    if not AUDIO_OFFSET_MIN_MS <= correction <= AUDIO_OFFSET_MAX_MS:
        res["reason"] = (f"Gemessener Versatz ({-correction:+d} ms) liegt außerhalb "
                         "des einstellbaren Bereichs.")
        return res
    spread = max(true_ms) - min(true_ms)
    res.update({
        "ok": True,
        "stable": spread <= STABLE_SPREAD_MS,
        "correction_ms": correction,
        "run_spread_ms": int(spread),
        "rest_ms": [t + correction for t in true_ms],
    })
    return res


# ---------------------------------------------------------------------------
# Gespeicherte Werte je Mikrofon
# ---------------------------------------------------------------------------
def stored_offset(store: dict | None, device_id: str | None) -> dict | None:
    """Eintrag {"ms", "source", "date"} für ein Mikrofon oder None."""
    if not isinstance(store, dict) or not device_id:
        return None
    entry = store.get(device_id)
    if not isinstance(entry, dict):
        return None
    try:
        ms = int(entry.get("ms", 0))
    except (TypeError, ValueError):
        return None
    ms = max(AUDIO_OFFSET_MIN_MS, min(AUDIO_OFFSET_MAX_MS, ms))
    return {"ms": ms, "source": str(entry.get("source", SOURCE_MANUAL)),
            "date": str(entry.get("date", ""))}


def remember_offset(store: dict | None, device_id: str | None, ms: int,
                    source: str, today: date | None = None) -> dict:
    """Gibt eine aktualisierte Kopie des Speichers zurück."""
    new = dict(store) if isinstance(store, dict) else {}
    if device_id:
        new[device_id] = {"ms": int(ms), "source": source,
                          "date": (today or date.today()).isoformat()}
    return new


def offset_note(entry: dict | None) -> str:
    """Kurztext neben dem Regler bzw. im Log."""
    if not entry:
        return "nicht kalibriert"
    if entry.get("source") == SOURCE_CALIBRATED:
        d = entry.get("date", "")
        try:
            y, m, dd = d.split("-")
            d = f"{dd}.{m}.{y}"
        except ValueError:
            pass
        return f"kalibriert am {d}" if d else "kalibriert"
    return "manuell eingestellt"


# ---------------------------------------------------------------------------
# Datei auswerten (FFmpeg)
# ---------------------------------------------------------------------------
def crop_filter(crop) -> str:
    """crop=(x, y, w, h) relativ zum aufgenommenen Bild -> FFmpeg-Filterteil."""
    if not crop:
        return ""
    x, y, w, h = (max(0, int(v)) for v in crop)
    return f"crop={max(2, w)}:{max(2, h)}:{x}:{y},"


def read_video_info(path: str, crop=None, max_seconds: float | None = None,
                    run=subprocess.run, timeout: float = 120.0):
    """(Bilder [(t, Helligkeit)], Schlüsselbilder [t]) des Blinkfelds."""
    cmd = [get_ffmpeg_path(), "-hide_banner", "-v", "info", "-nostats"]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd += ["-i", path, "-map", "0:v:0",
            "-vf", f"{crop_filter(crop)}scale=32:18,format=gray,showinfo",
            "-f", "null", "-"]
    proc = run(cmd, capture_output=True, timeout=timeout, **get_subprocess_flags())
    text = proc.stderr.decode("utf-8", errors="ignore")
    return parse_showinfo(text), parse_keyframes(text)


def read_audio_onsets(path: str, max_seconds: float | None = None,
                      run=subprocess.run, timeout: float = 120.0) -> list[float]:
    """Anfänge der 1-kHz-Pieptöne in der Tonspur (Zeit wie im Player)."""
    cmd = [get_ffmpeg_path(), "-hide_banner", "-v", "error"]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd += ["-i", path, "-map", "0:a:0",
            "-af", (f"bandpass=f={int(CALIB_FREQ_HZ)}:width_type=h:width=200,"
                    f"aresample={ANALYSIS_RATE}:async=1:first_pts=0"),
            "-ac", "1", "-f", "s16le", "-"]
    proc = run(cmd, capture_output=True, timeout=timeout, **get_subprocess_flags())
    samples = array.array("h")
    data = proc.stdout or b""
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    return beep_onsets(envelope(samples))


def analyze_file(path: str, crop=None, run=subprocess.run, min_pairs: int = MIN_PAIRS,
                 expected: int = CALIB_BEEPS) -> dict:
    """Misst Blitze und Pieptöne in einer Datei und wertet aus."""
    frames, _keys = read_video_info(path, crop, run=run)
    return evaluate(flash_onsets(frames), read_audio_onsets(path, run=run),
                    min_pairs=min_pairs, expected=expected)


# ---------------------------------------------------------------------------
# Tonausgabe
# ---------------------------------------------------------------------------
def _wasapi_default_output():
    """Index des WASAPI-Standardausgabegeräts (geringe, genaue Latenz) oder None."""
    try:
        import sounddevice as sd
        for api in sd.query_hostapis():
            idx = api.get("default_output_device", -1)
            if "WASAPI" in str(api.get("name", "")) and idx is not None and idx >= 0:
                return idx
    except Exception:
        pass
    return None


class BeepPlayer:
    """
    Spielt Pieptöne über sounddevice (RawOutputStream, ohne NumPy) und
    meldet für jeden den Zeitpunkt, an dem er aus dem Lautsprecher kommt:
    on_beep(perf_counter-Zeitpunkt).

    Nach dem Lauf für das Protokoll: device_info, latency_s, dac_beeps
    (wie viele Zeitpunkte aus outputBufferDacTime stammen).
    """

    def __init__(self, on_beep, beeps: int = CALIB_BEEPS, interval: float = CALIB_INTERVAL_S,
                 rate: int = CALIB_SAMPLE_RATE):
        self._on_beep = on_beep
        self._rate = rate
        self._pcm = make_beep_pcm(rate, beeps=beeps, interval=interval)
        self._starts = beep_start_samples(rate, beeps, interval)
        self._pos = 0
        self._stream = None
        self.done = threading.Event()
        self.latency_s = 0.0
        self.device_info = ""
        self.dac_beeps = 0

    def _callback(self, outdata, frames, time_info, status):
        import sounddevice as sd
        start_byte = self._pos * 2
        chunk = self._pcm[start_byte:start_byte + frames * 2]
        if len(chunk) < frames * 2:
            chunk = chunk + bytes(frames * 2 - len(chunk))
        outdata[:] = chunk
        now = time.perf_counter()
        dac = getattr(time_info, "outputBufferDacTime", 0.0) or 0.0
        cur = getattr(time_info, "currentTime", 0.0) or 0.0
        for s in self._starts:
            if self._pos <= s < self._pos + frames:
                offset = (s - self._pos) / self._rate
                if dac > 0 and cur > 0 and dac >= cur:
                    delay = dac - cur + offset
                    self.dac_beeps += 1
                else:
                    delay = self.latency_s + offset
                try:
                    self._on_beep(now + delay)
                except Exception:
                    pass
        self._pos += frames
        if self._pos * 2 >= len(self._pcm):
            raise sd.CallbackStop

    def start(self):
        import sounddevice as sd
        wasapi = _wasapi_default_output()
        attempts = []
        if wasapi is not None:
            try:
                # Mono/48 kHz notfalls von Windows umrechnen lassen
                attempts.append((wasapi, sd.WasapiSettings(auto_convert=True)))
            except Exception:
                pass
            attempts.append((wasapi, None))
        attempts.append((None, None))   # Standardgerät der Standard-Schnittstelle
        last_error = None
        for device, extra in attempts:
            try:
                self._stream = sd.RawOutputStream(
                    samplerate=self._rate, channels=1, dtype="int16",
                    device=device, latency="low", callback=self._callback,
                    finished_callback=self.done.set, extra_settings=extra,
                )
                lat = self._stream.latency
                self.latency_s = float(lat[1] if isinstance(lat, (tuple, list)) else lat)
                try:
                    dev = self._stream.device
                    dev = dev[1] if isinstance(dev, (tuple, list)) else dev
                    info = sd.query_devices(dev)
                    api = sd.query_hostapis(info["hostapi"])["name"]
                    self.device_info = f"{info['name']} [{api}]"
                except Exception:
                    self.device_info = str(device)
                self._stream.start()
                return
            except Exception as exc:
                last_error = exc
                self._stream = None
        raise RuntimeError(f"Tonausgabe nicht möglich: {last_error}")

    def stop(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self.done.set()


def play_signal(player, cancelled: threading.Event, beeps: int, interval: float) -> None:
    """Startet player und wartet (abbrechbar), bis das Signal durch ist."""
    player.start()
    limit = time.monotonic() + signal_seconds(beeps, interval) + 5.0
    while not player.done.wait(0.1):
        if cancelled.is_set():
            raise InterruptedError
        if time.monotonic() > limit:
            break
    player.stop()


def stop_ffmpeg(process, timeout: float = 8.0):
    """FFmpeg freundlich mit 'q' beenden, notfalls hart."""
    try:
        if process.stdin:
            process.stdin.write(b"q")
            process.stdin.flush()
            process.stdin.close()
        process.wait(timeout=timeout)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Protokoll
# ---------------------------------------------------------------------------
def format_calibration_log(device: str, settings: dict, crop, runs: list[dict],
                           summary: dict, player_info: dict, kept_files: list[str]) -> str:
    area = (f"Bereich {settings.get('region')}" if settings.get("mode_region") else "Vollbild")
    lines = [
        "ScreenRec Pro – Ton-Kalibrierung",
        f"Zeitpunkt:      {datetime.now().isoformat(timespec='seconds')}",
        f"Mikrofon:       {device}",
        f"Aufnahme:       {settings.get('fps')} fps, {settings.get('encoder')}, "
        f"Preset {settings.get('preset')}, {area}",
        f"Blinkfeld:      {crop}",
        f"Tonausgabe:     {player_info.get('device', '?')}, gemeldete Latenz "
        f"{player_info.get('latency_ms', '?')} ms, Zeitpunkte aus DAC-Zeit: "
        f"{player_info.get('dac_beeps', '?')}",
        "",
    ]
    for i, r in enumerate(runs, 1):
        head = f"Durchlauf {i}: Versatz angewendet {int(r.get('applied_ms', 0)):+d} ms -> "
        if r.get("ok"):
            head += (f"gemessen {r['lateness_ms']:+d} ms, Streuung {r['spread_ms']} ms, "
                     f"{r['pairs']} Paare")
        else:
            head += f"FEHLER: {r.get('reason')}"
        lines.append(head)
        lines.append(f"   Blitze: {r.get('flash_times')}")
        lines.append(f"   Töne:   {r.get('beep_times')}")
    lines.append("")
    if summary.get("ok"):
        lines.append(f"Echte Verspätung je Durchlauf: {summary['true_ms']} ms "
                     f"(Schwankung {summary['run_spread_ms']} ms)")
        lines.append(f"Ergebnis: Ton-Versatz {summary['correction_ms']:+d} ms, "
                     f"{'stabil' if summary['stable'] else 'SCHWANKEND'}; "
                     f"Rest je Durchlauf {summary['rest_ms']} ms")
    else:
        lines.append(f"Ergebnis: FEHLGESCHLAGEN – {summary.get('reason')}")
    if kept_files:
        lines.append("Messaufnahmen behalten: " + ", ".join(kept_files))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class CalibrationThread(threading.Thread):
    """
    Führt die Kalibrierung im Hintergrund aus.

    :param record_settings: Argumente für build_record_command wie bei der
                            echten Aufnahme (fps, encoder, preset, mode_region,
                            region, audio_device, gain, denoise, screen_size)
    :param crop:            Blinkfeld relativ zum aufgenommenen Bild
    :param log_dir:         Ordner für Protokoll (und ggf. Messaufnahmen)
    :param on_flash:        Callback(perf_counter-Zeitpunkt) - GUI blitzt dann
    :param on_progress:     Callback(text)
    :param on_finish:       Callback(summary aus combine_runs + "log_path")
    :param on_error:        Callback(text)
    """

    def __init__(self, record_settings: dict, crop, log_dir: str | None = None,
                 runs: int = CALIB_RUNS, on_flash=None, on_progress=None,
                 on_finish=None, on_error=None, player_factory=BeepPlayer,
                 popen=subprocess.Popen, analyze=analyze_file):
        super().__init__(daemon=True)
        self.settings = dict(record_settings)
        self.crop = crop
        self.log_dir = log_dir
        self.runs = runs
        self.on_flash, self.on_progress = on_flash, on_progress
        self.on_finish, self.on_error = on_finish, on_error
        self._player_factory, self._popen, self._analyze = player_factory, popen, analyze
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None
        self._player = None
        self.player_info: dict = {}

    def _emit(self, cb, *args):
        if cb:
            try:
                cb(*args)
            except Exception:
                pass

    def cancel(self):
        with self._lock:
            self._cancelled.set()
            process, player = self._process, self._player
        if player is not None:
            player.stop()
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass

    def kill(self):
        self.cancel()

    def _sleep(self, seconds: float):
        if self._cancelled.wait(seconds):
            raise InterruptedError

    def _one_run(self, index: int, path: str, applied_ms: int) -> dict:
        s = self.settings
        cmd = build_record_command(
            path, fps=str(s.get("fps", "30")), encoder=s.get("encoder", "libx264"),
            preset=s.get("preset", "ultrafast"), mode_region=bool(s.get("mode_region")),
            region=s.get("region"), audio_device=s.get("audio_device"),
            gain=float(s.get("gain", 1.0)), denoise=bool(s.get("denoise", False)),
            screen_size=s.get("screen_size"), audio_offset_ms=applied_ms,
        )
        with self._lock:
            if self._cancelled.is_set():
                raise InterruptedError
            self._process = self._popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, **get_subprocess_flags(),
            )
            process = self._process
        stderr_tail = []

        def read_stderr():
            try:
                for raw in iter(process.stderr.readline, b""):
                    stderr_tail.append(raw.decode("utf-8", errors="ignore").rstrip())
                    del stderr_tail[:-10]
            except Exception:
                pass

        reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        self._emit(self.on_progress, f"Durchlauf {index}/{self.runs}: Aufnahme startet ...")
        self._sleep(CALIB_LEAD_S)
        if process.poll() is not None:
            raise RuntimeError("FFmpeg wurde beim Start der Messung beendet:\n"
                               + "\n".join(stderr_tail[-5:]))

        player = self._player_factory(lambda due: self._emit(self.on_flash, due))
        with self._lock:
            if self._cancelled.is_set():
                raise InterruptedError
            self._player = player
        self._emit(self.on_progress, f"Durchlauf {index}/{self.runs}: Pieptöne ...")
        play_signal(player, self._cancelled, CALIB_BEEPS, CALIB_INTERVAL_S)
        self.player_info = {
            "device": getattr(player, "device_info", ""),
            "latency_ms": int(round(getattr(player, "latency_s", 0.0) * 1000)),
            "dac_beeps": getattr(player, "dac_beeps", 0),
        }
        with self._lock:
            self._player = None
        self._sleep(CALIB_TAIL_S)
        stop_ffmpeg(process)
        reader.join(timeout=2)
        with self._lock:
            self._process = None
        if self._cancelled.is_set():
            raise InterruptedError
        self._emit(self.on_progress, f"Durchlauf {index}/{self.runs}: Auswertung ...")
        result = dict(self._analyze(path, self.crop))
        result["applied_ms"] = applied_ms
        return result

    def run(self):
        paths: list[str] = []
        runs: list[dict] = []
        try:
            applied = 0
            for i in range(1, self.runs + 1):
                fd, path = tempfile.mkstemp(prefix=f"screenrecpro_kalib{i}_", suffix=".mp4")
                os.close(fd)
                paths.append(path)
                runs.append(self._one_run(i, path, applied))
                # Ab Durchlauf 2 mit dem bisher besten Wert aufnehmen (Gegenprobe)
                good = [r for r in runs if r.get("ok")]
                if good:
                    applied = -round10(median(
                        [r["lateness_ms"] - r["applied_ms"] for r in good]))
                    applied = max(AUDIO_OFFSET_MIN_MS, min(AUDIO_OFFSET_MAX_MS, applied))
                elif i >= 2:
                    break   # zwei Fehlschläge: weitere Durchläufe bringen nichts
            summary = combine_runs(runs)
            keep = (not summary.get("ok")) or (not summary.get("stable"))
            summary["log_path"] = self._write_log(runs, summary, paths if keep else [])
            self._emit(self.on_finish, summary)
        except InterruptedError:
            pass
        except Exception as exc:
            if not self._cancelled.is_set():
                try:
                    self._write_log(runs, {"ok": False, "reason": str(exc)}, paths)
                except Exception:
                    pass
                self._emit(self.on_error, f"Kalibrierung fehlgeschlagen: {exc}")
        finally:
            with self._lock:
                player, process = self._player, self._process
            if player is not None:
                player.stop()
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _write_log(self, runs, summary, keep_paths) -> str | None:
        """Protokoll in log_dir; behaltene Messaufnahmen werden dorthin kopiert."""
        if not self.log_dir or not os.path.isdir(self.log_dir):
            return None
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        kept = []
        for i, p in enumerate(keep_paths, 1):
            if os.path.exists(p) and os.path.getsize(p) > 0:
                target = os.path.join(self.log_dir, f"Kalibrierung_{stamp}_lauf{i}.mp4")
                try:
                    shutil.copyfile(p, target)
                    kept.append(os.path.basename(target))
                except OSError:
                    pass
        log_path = os.path.join(self.log_dir, f"Kalibrierung_{stamp}_log.txt")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(format_calibration_log(
                    str(self.settings.get("audio_device")), self.settings, self.crop,
                    runs, summary, self.player_info, kept))
            return log_path
        except OSError:
            return None
