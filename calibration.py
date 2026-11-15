"""
calibration.py
==============
"Ton kalibrieren" - misst den Ton-Bild-Versatz eines Mikrofons (GUI-frei).

Ablauf (siehe plan-kalibrierung.md):
  1. FFmpeg nimmt mit DERSELBEN Kette wie eine echte Aufnahme auf
     (build_record_command), Bereich = kleines Kalibrierfenster der GUI.
  2. Über die Lautsprecher laufen CALIB_BEEPS kurze 1-kHz-Pieptöne. Für
     jeden meldet der Audio-Callback, wann er tatsächlich aus dem
     Lautsprecher kommt (outputBufferDacTime); genau dann lässt die GUI
     das Fenster weiß aufblitzen. So verfälscht die Ausgabelatenz der
     Lautsprecher die Messung nicht.
  3. Die Datei wird nur mit FFmpeg + reinem Python ausgewertet (kein
     NumPy): Bild-Helligkeit per showinfo, Ton per bandpass -> PCM.
  4. Ergebnis: Median(Ton - Bild). Die Korrektur ist der negative Wert,
     auf 10 ms gerundet - sie landet im vorhandenen Ton-Versatz-Regler.

Die Auswertefunktionen sind reine Funktionen und ohne Hardware testbar.
"""

import array
import math
import os
import sys
import re
import subprocess
import tempfile
import threading
import time
from datetime import date

from config import AUDIO_OFFSET_MAX_MS, AUDIO_OFFSET_MIN_MS
from ffmpeg_utils import build_record_command, get_ffmpeg_path
from platform_utils import get_subprocess_flags

# ---------------------------------------------------------------------------
# Parameter
# ---------------------------------------------------------------------------
CALIB_BEEPS = 6
CALIB_INTERVAL_S = 2.0          # Abstand der Pieptöne (Paarung bis ±0,95 s eindeutig)
CALIB_BEEP_S = 0.12             # Länge eines Pieptons
CALIB_LEAD_S = 3.0              # FFmpeg-Anlauf, bevor der erste Ton kommt
CALIB_TAIL_S = 1.2              # Nachlauf nach dem letzten Ton
CALIB_FLASH_S = 0.15            # Dauer eines Blitzes (GUI)
CALIB_SAMPLE_RATE = 48000
CALIB_FREQ_HZ = 1000.0

MIN_PAIRS = 4                   # mindestens so viele erkannte Paare
MAX_SPREAD_MS = 70              # größte erlaubte Streuung der Einzelwerte
PAIR_WINDOW_S = 0.95            # Ton muss so nah am Blitz liegen

ANALYSIS_RATE = 8000            # Ton-Auswertung: Mono, 8 kHz
ENVELOPE_BLOCK_S = 0.01         # Hüllkurve in 10-ms-Blöcken
BEEP_THRESHOLD = 0.25           # Anteil zwischen Rauschen und lautem Pegel

SOURCE_CALIBRATED = "kalibriert"
SOURCE_MANUAL = "manuell"


# ---------------------------------------------------------------------------
# Signal erzeugen
# ---------------------------------------------------------------------------
def beep_start_samples(rate: int = CALIB_SAMPLE_RATE) -> list[int]:
    """Startsample jedes Pieptons im erzeugten Tonsignal (0,3 s Vorlauf)."""
    return [int(round((0.3 + k * CALIB_INTERVAL_S) * rate)) for k in range(CALIB_BEEPS)]


def make_beep_pcm(rate: int = CALIB_SAMPLE_RATE, amplitude: float = 0.5) -> bytes:
    """
    Mono-PCM (int16, little endian) mit CALIB_BEEPS Pieptönen und je 5 ms
    Ein-/Ausblendung gegen Knacken. Ohne NumPy (array-Modul).
    """
    starts = beep_start_samples(rate)
    beep_len = int(CALIB_BEEP_S * rate)
    total = starts[-1] + beep_len + int(0.3 * rate)
    pcm = array.array("h", bytes(2 * total))
    fade = max(1, int(0.005 * rate))
    peak = int(32767 * amplitude)
    w = 2.0 * math.pi * CALIB_FREQ_HZ / rate
    for start in starts:
        for i in range(beep_len):
            env = min(1.0, i / fade, (beep_len - 1 - i) / fade)
            pcm[start + i] = int(peak * env * math.sin(w * i))
    if pcm.itemsize != 2:            # sehr exotische Plattform
        raise RuntimeError("int16-Array nicht verfügbar")
    return pcm.tobytes() if sys.byteorder == "little" else _swap(pcm)


def _swap(pcm: array.array) -> bytes:
    pcm = array.array("h", pcm)
    pcm.byteswap()
    return pcm.tobytes()


def total_signal_seconds(rate: int = CALIB_SAMPLE_RATE) -> float:
    return len(make_beep_pcm(rate)) / 2 / rate


# ---------------------------------------------------------------------------
# Auswertung (reine Funktionen)
# ---------------------------------------------------------------------------
_SHOWINFO_RE = re.compile(r"pts_time:\s*([-\d.]+).*?mean:\[\s*([\d.]+)")


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


def flash_onsets(frames: list[tuple[float, float]], min_contrast: float = 60.0,
                 min_gap_s: float = 1.0) -> list[float]:
    """
    Zeitpunkte, an denen das Kalibrierfenster hell wird. Schwelle in der
    Mitte zwischen dunkelstem und hellstem Bild; ohne ausreichenden
    Kontrast (kein Blitz im Bild) leere Liste.
    """
    if len(frames) < 2:
        return []
    values = [v for _, v in frames]
    lo, hi = min(values), max(values)
    if hi - lo < min_contrast:
        return []
    thr = (lo + hi) / 2.0
    onsets, last = [], -1e9
    for (t0, v0), (t1, v1) in zip(frames, frames[1:]):
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
    Schwelle: ein Viertel des Abstands zwischen Grundrauschen (Median) und lautem
    Pegel (99,5-%-Wert). Ist der Ton nicht deutlich lauter als das
    Rauschen, wird nichts gemeldet.
    """
    if len(env) < 10:
        return []
    ordered = sorted(env)
    noise = ordered[len(ordered) // 2]
    loud = ordered[min(len(ordered) - 1, int(len(ordered) * 0.995))]
    if loud < max(noise, 1.0) * min_snr:
        return []
    # Niedrige Schwelle (25 %): Viele Mikrofone regeln den Pegel nach dem
    # Einsatz erst hoch (AGC/Hall) - bei 50 % lag der erkannte Anfang
    # in echten Aufnahmen bis zu 70 ms zu spät.
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


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def evaluate(video: list[float], audio: list[float]) -> dict:
    """
    Gesamtergebnis der Kalibrierung.

    Rückgabe: {"ok", "reason", "lateness_ms", "correction_ms", "pairs",
               "spread_ms", "flashes", "beeps"}
      lateness_ms   > 0: Ton kommt zu spät
      correction_ms Wert für den Ton-Versatz-Regler (= -lateness, 10-ms-Raster)
    """
    res = {"ok": False, "reason": "", "lateness_ms": None, "correction_ms": None,
           "pairs": 0, "spread_ms": None, "flashes": len(video), "beeps": len(audio)}
    if not audio:
        res["reason"] = ("Kein Piepton erkannt. Bitte Lautsprecher einschalten, "
                         "Lautstärke erhöhen und keinen geschlossenen Kopfhörer verwenden.")
        return res
    if not video:
        res["reason"] = ("Blinken im Bild nicht erkannt. Bitte das Kalibrierfenster "
                         "während der Messung nicht verdecken.")
        return res
    offsets = pair_offsets(video, audio)
    res["pairs"] = len(offsets)
    if len(offsets) < MIN_PAIRS:
        res["reason"] = (f"Nur {len(offsets)} von {CALIB_BEEPS} Messpunkten erkannt "
                         f"(mindestens {MIN_PAIRS} nötig). Bitte in ruhiger Umgebung wiederholen.")
        return res
    ms = [o * 1000.0 for o in offsets]
    spread = max(ms) - min(ms)
    lateness = _median(ms)
    res["spread_ms"] = int(round(spread))
    res["lateness_ms"] = int(round(lateness))
    if spread > MAX_SPREAD_MS:
        res["reason"] = (f"Messung zu unruhig (Streuung {spread:.0f} ms). "
                         "Bitte in ruhiger Umgebung wiederholen.")
        return res
    correction = -int(round(lateness / 10.0)) * 10
    if not AUDIO_OFFSET_MIN_MS <= correction <= AUDIO_OFFSET_MAX_MS:
        res["reason"] = (f"Gemessener Versatz ({lateness:+.0f} ms) liegt außerhalb "
                         f"des einstellbaren Bereichs.")
        return res
    res["correction_ms"] = correction
    res["ok"] = True
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
def analyze_file(path: str, run=subprocess.run, timeout: float = 60.0) -> dict:
    """Misst Blitze und Pieptöne in der Kalibrierdatei und wertet aus."""
    ffmpeg = get_ffmpeg_path()
    flags = get_subprocess_flags()

    video_run = run(
        [ffmpeg, "-hide_banner", "-v", "info", "-nostats", "-i", path, "-map", "0:v:0",
         "-vf", "scale=32:18,format=gray,showinfo", "-f", "null", "-"],
        capture_output=True, timeout=timeout, **flags,
    )
    frames = parse_showinfo(video_run.stderr.decode("utf-8", errors="ignore"))

    audio_run = run(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", path, "-map", "0:a:0",
         "-af", (f"bandpass=f={int(CALIB_FREQ_HZ)}:width_type=h:width=200,"
                 f"aresample={ANALYSIS_RATE}:async=1:first_pts=0"),
         "-ac", "1", "-f", "s16le", "-"],
        capture_output=True, timeout=timeout, **flags,
    )
    samples = array.array("h")
    data = audio_run.stdout or b""
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder != "little":
        samples.byteswap()

    return evaluate(flash_onsets(frames), beep_onsets(envelope(samples)))


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
    Spielt das Kalibriersignal über sounddevice (RawOutputStream, ohne
    NumPy) und meldet für jeden Piepton den Zeitpunkt, an dem er aus dem
    Lautsprecher kommt: on_beep(perf_counter-Zeitpunkt).
    """

    def __init__(self, on_beep, rate: int = CALIB_SAMPLE_RATE):
        self._on_beep = on_beep
        self._rate = rate
        self._pcm = make_beep_pcm(rate)
        self._starts = beep_start_samples(rate)
        self._pos = 0
        self._stream = None
        self.done = threading.Event()
        self.latency_s = 0.0

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
        last_error = None
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
        for device, extra in attempts:
            try:
                self._stream = sd.RawOutputStream(
                    samplerate=self._rate, channels=1, dtype="int16",
                    device=device, latency="low", callback=self._callback,
                    finished_callback=self.done.set, extra_settings=extra,
                )
                lat = self._stream.latency
                self.latency_s = float(lat[1] if isinstance(lat, (tuple, list)) else lat)
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


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class CalibrationThread(threading.Thread):
    """
    Führt die Kalibrierung im Hintergrund aus.

    :param audio_device: FFmpeg-ID des Mikrofons
    :param region:       (x, y, w, h) des hellen Bereichs im Kalibrierfenster
    :param on_flash:     Callback(perf_counter-Zeitpunkt) - GUI blitzt dann
    :param on_progress:  Callback(text)
    :param on_finish:    Callback(result-dict aus evaluate)
    :param on_error:     Callback(text)
    """

    def __init__(self, audio_device, region, fps, screen_size,
                 on_flash=None, on_progress=None, on_finish=None, on_error=None,
                 player_factory=BeepPlayer, popen=subprocess.Popen, analyze=analyze_file):
        super().__init__(daemon=True)
        self.audio_device = audio_device
        self.region = region
        self.fps = str(fps)
        self.screen_size = screen_size
        self.on_flash, self.on_progress = on_flash, on_progress
        self.on_finish, self.on_error = on_finish, on_error
        self._player_factory, self._popen, self._analyze = player_factory, popen, analyze
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None
        self._player = None

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

    def run(self):
        fd, path = tempfile.mkstemp(prefix="screenrecpro_kalib_", suffix=".mp4")
        os.close(fd)
        try:
            self._emit(self.on_progress, "Messung wird vorbereitet ...")
            cmd = build_record_command(
                path, fps=self.fps, encoder="libx264", preset="ultrafast",
                mode_region=True, region=self.region, audio_device=self.audio_device,
                gain=1.0, denoise=False, screen_size=self.screen_size, audio_offset_ms=0,
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

            self._emit(self.on_progress, "Aufnahme läuft - bitte ruhig sein ...")
            self._sleep(CALIB_LEAD_S)
            if process.poll() is not None:
                raise RuntimeError("FFmpeg ist beim Start der Messung beendet worden:\n"
                                   + "\n".join(stderr_tail[-5:]))

            player = self._player_factory(lambda due: self._emit(self.on_flash, due))
            with self._lock:
                if self._cancelled.is_set():
                    raise InterruptedError
                self._player = player
            player.start()
            self._emit(self.on_progress, "Pieptöne laufen ...")
            limit = time.monotonic() + total_signal_seconds() + 5.0
            while not player.done.wait(0.1):
                if self._cancelled.is_set():
                    raise InterruptedError
                if time.monotonic() > limit:
                    break
            player.stop()
            self._sleep(CALIB_TAIL_S)

            self._stop_ffmpeg(process)
            reader.join(timeout=2)
            if self._cancelled.is_set():
                raise InterruptedError
            self._emit(self.on_progress, "Messung wird ausgewertet ...")
            result = self._analyze(path)
            self._emit(self.on_finish, result)
        except InterruptedError:
            pass
        except Exception as exc:
            if not self._cancelled.is_set():
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
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _stop_ffmpeg(process):
        try:
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.flush()
                process.stdin.close()
            process.wait(timeout=8)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
