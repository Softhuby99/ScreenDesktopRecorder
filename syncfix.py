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
     Erst wenn das gelingt, ersetzt die neue Datei die alte. Bei jedem
     Problem bleibt die Originalaufnahme unverändert.
"""

import os
import subprocess
import threading

from calibration import (
    BeepPlayer, evaluate, flash_onsets, play_signal, read_audio_onsets, read_video_info,
)
from ffmpeg_utils import get_ffmpeg_path
from platform_utils import get_subprocess_flags

MARKER_BEEPS = 3
MARKER_INTERVAL_S = 2.0
MARKER_LEAD_S = 1.5             # nach dem Aufnahmestart (Mikrofon läuft an)
MARKER_MIN_PAIRS = 2
MARKER_ANALYSIS_EXTRA_S = 3.0   # hinter dem Markerende noch einlesen (Schlüsselbild)
CUT_MARGIN_S = 0.25             # Abstand Markerende -> frühester Schnitt
SEEK_EPS_S = 0.0                # genau auf das Schlüsselbild (gemessen: +5 ms verschob
                                # das Bild bei Stream-Kopie um ein ganzes Bild)


# ---------------------------------------------------------------------------
# Reine Funktionen
# ---------------------------------------------------------------------------
def choose_cut(keyframes: list[float], marker_end_s: float,
               margin_s: float = CUT_MARGIN_S) -> float | None:
    """Erstes Schlüsselbild frühestens margin_s nach dem Markerende."""
    candidates = [k for k in keyframes if k >= marker_end_s + margin_s]
    return min(candidates) if candidates else None


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


def marker_flashes(flashes: list[float], marker_end_s: float, slack_s: float = 0.6) -> list[float]:
    """Nur Blitze bis kurz nach dem Markerende zählen (danach ist es Bildinhalt)."""
    return [t for t in flashes if t <= marker_end_s + slack_s]


def analyze_marker(path: str, crop, marker_end_s: float, run=subprocess.run) -> dict:
    """
    Wertet den Marker am Anfang der Datei aus.
    Rückgabe wie calibration.evaluate, zusätzlich "cut_s" (oder None).
    """
    window = marker_end_s + MARKER_ANALYSIS_EXTRA_S
    frames, keys = read_video_info(path, crop, max_seconds=window, run=run)
    flashes = marker_flashes(flash_onsets(frames), marker_end_s)
    beeps = [t for t in read_audio_onsets(path, max_seconds=window, run=run)
             if t <= marker_end_s + 1.5]
    res = evaluate(flashes, beeps, min_pairs=MARKER_MIN_PAIRS, expected=MARKER_BEEPS)
    res["cut_s"] = choose_cut(keys, marker_end_s)
    if res["ok"] and res["cut_s"] is None:
        res["ok"] = False
        res["reason"] = "Kein Schnittpunkt hinter dem Marker gefunden."
    return res


def describe_fix(result: dict) -> str:
    """Zeile für das Aufnahme-Log."""
    if result.get("fixed"):
        late = result["lateness_ms"]
        what = (f"Ton {late} ms zu spät" if late > 0 else
                f"Ton {-late} ms zu früh" if late < 0 else "Ton bereits synchron")
        return (f"Sync-Marker:    {what} gemessen -> korrigiert "
                f"(Streuung {result.get('spread_ms')} ms, {result.get('pairs')} Paare); "
                f"Anfang {result['cut_s']:.2f} s mit dem Marker entfernt")
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
        info = {"ok": False, "error": "", "device": "", "latency_ms": None}
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
            info.update(ok=not self._cancelled.is_set(),
                        device=getattr(player, "device_info", ""),
                        latency_ms=int(round(getattr(player, "latency_s", 0.0) * 1000)))
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
                 analyze=analyze_marker, popen=subprocess.Popen):
        super().__init__(daemon=True)
        self.path, self.crop, self.marker_end_s = path, crop, marker_end_s
        self.on_finish = on_finish
        self._analyze, self._popen = analyze, popen
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
            if self._cancelled.is_set():
                result["reason"] = "abgebrochen"
                return
            if not result.get("ok"):
                return
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
