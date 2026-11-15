"""
syncfix.py
==========
Sync-Marker (Weg B): Ton-Bild-Versatz IN JEDER Aufnahme messen und danach
genau diese Datei korrigieren (GUI-frei).

Für Mikrofone, deren Verzögerung von Aufnahme zu Aufnahme schwankt - dort
hilft ein vorher kalibrierter fester Wert nicht (siehe calibration.py).

Ablauf:
  1. Gleich nach dem Aufnahmestart blinkt ein Feld im aufgenommenen Bereich
     MARKER_BEEPS-mal, gleichzeitig piept es über die Lautsprecher
     (MarkerThread, gleiche Technik wie die Kalibrierung). Die Aufnahme hat
     in dieser Zeit dichte Schlüsselbilder (ffmpeg_utils, marker_keyframes).
  2. Nach dem Stoppen misst SyncFixThread im Anfang der Datei, wie spät
     der Ton kommt, wählt das erste Schlüsselbild nach dem Marker als
     Schnittpunkt und schreibt die Datei neu:
       Bild ab Schnittpunkt unverändert kopiert (keine Neukodierung),
       Ton ab Schnittpunkt + Verspätung, neu als AAC kodiert (genau auf
       die Probe, nicht nur auf AAC-Rahmen).
     Die neue Datei wird geprüft (Bild- und Tonspur vorhanden, Dauer
     plausibel). Erst dann ersetzt sie die alte. Bei jedem Problem bleibt
     die Originalaufnahme unverändert.

Zeitbezug: Die Aufnahmeuhr der App (recorder.get_elapsed) startet erst
START_CHECK_SECONDS nach FFmpeg; die Dateizeit kann ihr also bis zu dieser
Spanne VORAUS sein. Deshalb werden die Marker-Blitze am Raster erkannt
(nicht an der App-Uhr), und der Schnitt richtet sich nach dem letzten
erkannten Blitz UND - mit Sicherheitsabstand - nach der App-Uhr.
"""

import os
import re
import subprocess
import threading

from calibration import (
    BeepPlayer, CALIB_BEEP_S, SIGNAL_PAD_S, evaluate, flash_onsets, play_signal,
    player_report, read_audio_onsets, read_video_info,
)
from ffmpeg_utils import get_ffmpeg_path
from platform_utils import get_subprocess_flags

MARKER_BEEPS = 3
MARKER_INTERVAL_S = 2.0
MARKER_LEAD_S = 1.5             # nach dem Aufnahmestart (Mikrofon läuft an)
MARKER_MIN_PAIRS = 2
MARKER_RASTER_TOL_S = 0.25      # Blitze müssen so genau im Raster liegen
MARKER_ANALYSIS_EXTRA_S = 5.0   # hinter dem Markerende (App-Uhr) noch einlesen
CLOCK_LEAD_S = 1.0              # Dateizeit kann der App-Uhr so weit voraus sein
                                # (recorder.START_CHECK_SECONDS = 0,8 s + Reserve)
CUT_MARGIN_S = CLOCK_LEAD_S + 0.25          # Abstand Markerende (App-Uhr) -> Schnitt
# Nach dem letzten Blitz läuft das Signal noch aus, dann schließt die GUI
# das Marker-Fenster - der Schnitt muss sicher dahinter liegen.
CUT_AFTER_LAST_FLASH_S = CALIB_BEEP_S + SIGNAL_PAD_S + 1.1
SEEK_EPS_S = 0.0                # genau auf das Schlüsselbild (gemessen: +5 ms verschob
                                # das Bild bei Stream-Kopie um ein ganzes Bild)


# ---------------------------------------------------------------------------
# Reine Funktionen
# ---------------------------------------------------------------------------
def choose_cut(keyframes: list[float], earliest_s: float) -> float | None:
    """Erstes Schlüsselbild ab earliest_s."""
    candidates = [k for k in keyframes if k >= earliest_s]
    return min(candidates) if candidates else None


def earliest_cut(marker_end_s: float, last_flash_s: float | None) -> float:
    """Frühester Schnitt: hinter App-Uhr-Ende UND hinter dem letzten Blitz."""
    bound = marker_end_s + CUT_MARGIN_S
    if last_flash_s is not None:
        bound = max(bound, last_flash_s + CUT_AFTER_LAST_FLASH_S)
    return bound


def pick_marker_flashes(onsets: list[float], count: int = MARKER_BEEPS,
                        interval: float = MARKER_INTERVAL_S,
                        tol: float = MARKER_RASTER_TOL_S) -> list[float]:
    """
    Die Marker-Blitze sind die früheste, längste Folge von Blitzen im
    Raster interval (höchstens count). Helligkeitswechsel des Bildinhalts
    (z. B. wenn das Marker-Fenster schließt) fallen so heraus.
    """
    best: list[float] = []
    for first in onsets:
        run = [first]
        for k in range(1, count):
            target = first + k * interval
            near = [t for t in onsets if abs(t - target) <= tol]
            if near:
                run.append(min(near, key=lambda t: abs(t - target)))
        if len(run) > len(best):
            best = run
    return best


def build_fix_command(src: str, dst: str, cut_s: float, lateness_ms: int) -> list:
    """
    Bild ab cut_s (Schlüsselbild, Stream-Kopie), Ton ab cut_s + Verspätung
    (neu kodiert) -> beide beginnen gemeinsam bei 0.
    """
    video_start = cut_s + SEEK_EPS_S
    audio_start = max(0.0, cut_s + lateness_ms / 1000.0)
    return [
        get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{video_start:.6f}", "-i", src,
        "-ss", f"{audio_start:.6f}", "-i", src,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        dst,
    ]


def analyze_marker(path: str, crop, marker_end_s: float, run=subprocess.run) -> dict:
    """
    Wertet den Marker am Anfang der Datei aus.
    Rückgabe wie calibration.evaluate, zusätzlich "cut_s" (oder None).
    """
    window = marker_end_s + MARKER_ANALYSIS_EXTRA_S
    frames, keys = read_video_info(path, crop, max_seconds=window, run=run)
    in_range = [t for t in flash_onsets(frames) if t <= marker_end_s + CLOCK_LEAD_S]
    flashes = pick_marker_flashes(in_range)
    beeps = read_audio_onsets(path, max_seconds=window, run=run)
    res = evaluate(flashes, beeps, min_pairs=MARKER_MIN_PAIRS, expected=MARKER_BEEPS)
    res["cut_s"] = choose_cut(keys, earliest_cut(marker_end_s, flashes[-1] if flashes else None))
    if res["ok"] and res["cut_s"] is None:
        res["ok"] = False
        res["reason"] = "Kein Schnittpunkt hinter dem Marker gefunden."
    return res


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")


def probe_file(path: str, run=subprocess.run, timeout: float = 30.0) -> dict:
    """{"duration": Sekunden|None, "video": bool, "audio": bool} per `ffmpeg -i`."""
    proc = run([get_ffmpeg_path(), "-hide_banner", "-i", path],
               capture_output=True, timeout=timeout, **get_subprocess_flags())
    text = (proc.stderr or b"").decode("utf-8", errors="ignore")
    m = _DURATION_RE.search(text)
    duration = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else None
    return {"duration": duration, "video": " Video: " in text, "audio": " Audio: " in text}


def check_fixed_file(original: dict, fixed: dict, cut_s: float,
                     tolerance_s: float = 2.0) -> str:
    """Leerer Text = neue Datei ist plausibel; sonst der Grund."""
    if not (fixed.get("video") and fixed.get("audio")):
        return "neue Datei hat nicht beide Spuren"
    if original.get("duration") is None or fixed.get("duration") is None:
        return "Dauer der neuen Datei nicht lesbar"
    expected = original["duration"] - cut_s
    if abs(fixed["duration"] - expected) > tolerance_s:
        return (f"Dauer der neuen Datei unplausibel ({fixed['duration']:.1f} s statt "
                f"ca. {expected:.1f} s)")
    return ""


def describe_fix(result: dict) -> str:
    """Zeile für das Aufnahme-Log."""
    if result.get("fixed"):
        late = result["lateness_ms"]
        what = (f"Ton {late} ms zu spät" if late > 0 else
                f"Ton {-late} ms zu früh" if late < 0 else "Ton bereits synchron")
        return (f"Sync-Marker:    {what} gemessen -> korrigiert "
                f"(Streuung {result.get('spread_ms')} ms, {result.get('pairs')} Paare, "
                f"darin {int(result.get('draw_delay_ms', 0)):+d} ms für verspätet "
                f"gezeichnetes Blinken); Anfang {result['cut_s']:.2f} s mit dem Marker entfernt")
    return f"Sync-Marker:    nicht korrigiert – {result.get('reason') or 'unbekannter Grund'} (Datei unverändert)"


# ---------------------------------------------------------------------------
# Marker abspielen (während der Aufnahme)
# ---------------------------------------------------------------------------
class MarkerThread(threading.Thread):
    """
    Spielt nach MARKER_LEAD_S die Marker-Pieptöne; on_flash(perf_counter-
    Zeitpunkt) je Ton, on_done(info) am Ende - info: {"ok", "error",
    "device", "latency_ms"}. cancel() bricht still ab (on_done mit ok=False).
    """

    def __init__(self, on_flash=None, on_done=None, player_factory=BeepPlayer):
        super().__init__(daemon=True)
        self.on_flash, self.on_done = on_flash, on_done
        self._player_factory = player_factory
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._player = None

    def cancel(self):
        with self._lock:
            self._cancelled.set()
            player = self._player
        if player is not None:
            player.stop()

    def kill(self):
        self.cancel()

    def run(self):
        info = {"ok": False, "error": "", "device": "", "latency_ms": None, "errors": []}
        try:
            if self._cancelled.wait(MARKER_LEAD_S):
                raise InterruptedError
            player = self._player_factory(
                lambda due: self.on_flash and self.on_flash(due),
                beeps=MARKER_BEEPS, interval=MARKER_INTERVAL_S,
            )
            with self._lock:
                if self._cancelled.is_set():
                    raise InterruptedError
                self._player = player
            play_signal(player, self._cancelled, MARKER_BEEPS, MARKER_INTERVAL_S)
            info.update(player_report(player))
            info["ok"] = not self._cancelled.is_set()
        except InterruptedError:
            info["error"] = "abgebrochen"
        except Exception as exc:
            info["error"] = str(exc)
        finally:
            with self._lock:
                player, self._player = self._player, None
            if player is not None:
                player.stop()
            if self.on_done:
                try:
                    self.on_done(info)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Datei korrigieren (nach dem Stoppen)
# ---------------------------------------------------------------------------
class SyncFixThread(threading.Thread):
    """
    Misst den Marker und schreibt die korrigierte Datei.
    on_finish(result): result wie analyze_marker, zusätzlich "fixed" (bool).
    cancel(): bricht ab, die Originaldatei bleibt unverändert.
    """

    def __init__(self, path: str, crop, marker_end_s: float, on_finish=None,
                 draw_delay_ms: int = 0, analyze=analyze_marker,
                 popen=subprocess.Popen, probe=probe_file):
        super().__init__(daemon=True)
        self.path, self.crop, self.marker_end_s = path, crop, marker_end_s
        self.on_finish = on_finish
        # Um so viel wurde das Blinken zu spät gezeichnet (siehe
        # calibration.draw_delay_ms) - wird in die Messung eingerechnet.
        self.draw_delay_ms = max(0, int(draw_delay_ms or 0))
        self._analyze, self._popen, self._probe = analyze, popen, probe
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None

    def cancel(self):
        with self._lock:
            self._cancelled.set()
            process = self._process
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass

    def kill(self):
        self.cancel()

    def _tmp_path(self) -> str:
        root, ext = os.path.splitext(self.path)
        return f"{root}.sync-tmp{ext or '.mp4'}"

    def run(self):
        result = {"ok": False, "fixed": False, "reason": ""}
        tmp = self._tmp_path()
        try:
            result = dict(self._analyze(self.path, self.crop, self.marker_end_s))
            result["fixed"] = False
            result["draw_delay_ms"] = self.draw_delay_ms
            if self._cancelled.is_set():
                result["reason"] = "abgebrochen"
                return
            if not result.get("ok"):
                return
            result["lateness_ms"] = int(result["lateness_ms"]) + self.draw_delay_ms
            cmd = build_fix_command(self.path, tmp, result["cut_s"], result["lateness_ms"])
            with self._lock:
                if self._cancelled.is_set():
                    result["reason"] = "abgebrochen"
                    return
                self._process = self._popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, **get_subprocess_flags(),
                )
                process = self._process
            _out, err = process.communicate()
            if self._cancelled.is_set():
                result["reason"] = "abgebrochen"
                return
            if process.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                text = (err or b"").decode("utf-8", errors="ignore").strip().splitlines()
                result["reason"] = "Neuschreiben fehlgeschlagen: " + (text[-1] if text else "?")
                return
            problem = check_fixed_file(self._probe(self.path), self._probe(tmp), result["cut_s"])
            if problem:
                result["reason"] = "Prüfung fehlgeschlagen: " + problem
                return
            if self._cancelled.is_set():
                result["reason"] = "abgebrochen"
                return
            os.replace(tmp, self.path)
            result["fixed"] = True
        except Exception as exc:
            result["reason"] = f"Fehler: {exc}"
        finally:
            if not result.get("fixed"):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if self.on_finish:
                try:
                    self.on_finish(result)
                except Exception:
                    pass
