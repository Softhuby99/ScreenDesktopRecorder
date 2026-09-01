"""
optimizer.py
============
Nachträgliche Verkleinerung bereits aufgenommener Videodateien.

Hintergrund: Eine LIVE-Aufnahme muss aus Zeitgründen mit einem schnellen
Preset kodieren (siehe config.py/ffmpeg_utils.py: "ultrafast" .. "faster")
- dem Encoder bleibt dabei kaum Zeit für aufwendige Bewegungssuche und
psychovisuelle Optimierung, was auf Kosten der Kompressionseffizienz geht.
Im Nachhinein, OHNE Echtzeit-Zwang, lässt sich dieselbe visuelle Qualität
mit einem sehr viel langsameren (aber effizienteren) Preset - oder einem
moderneren Codec wie H.265 - in einer deutlich kleineren Datei erreichen.

Dieses Modul ist bewusst komplett unabhängig vom Aufnahmepfad
(recorder.py/ffmpeg_utils.build_record_command): es nimmt eine FERTIGE
Videodatei entgegen und schreibt eine neue, kleinere Datei daneben - die
Originaldatei wird nie verändert oder gelöscht.
"""

import os
import re
import subprocess
import threading

from ffmpeg_utils import get_ffmpeg_path
from platform_utils import get_subprocess_flags

# ============================================================================
# 1) OPTIMIERUNGS-PROFILE
# ============================================================================
# CRF-Werte sind bewusst so gewählt, dass die visuelle Qualität gegenüber
# einer "schnell" kodierten Live-Aufnahme sichtbar GLEICH BLEIBT oder sich
# sogar leicht verbessert (langsamere Presets nutzen den Bit-Spielraum
# klüger) - der Gewinn kommt fast ausschließlich aus dem langsameren
# Preset bzw. dem effizienteren Codec, nicht aus einer aggressiveren
# Qualitätsabsenkung. "H.265 CRF 26 ≈ H.264 CRF 23" ist eine verbreitete
# Faustregel für vergleichbare wahrgenommene Qualität, kein Messwert -
# das tatsächliche Ergebnis hängt immer vom Inhalt ab.
OPTIMIZE_PROFILES = [
    {
        "id": "balanced",
        "label": "Ausgewogen (empfohlen)",
        "description": (
            "H.264, langsameres Preset – deutlich kleinere Datei, "
            "kaum sichtbarer Qualitätsunterschied. Guter Standardfall."
        ),
        "encoder": "libx264",
        "preset": "slow",
        "crf": "23",
    },
    {
        "id": "smaller_h265",
        "label": "Kleiner (H.265)",
        "description": (
            "Modernerer Codec – spürbar kleinere Datei bei sehr "
            "ähnlicher Qualität. Braucht länger als \"Ausgewogen\", "
            "und nicht jeder Player/jedes Altgerät unterstützt H.265."
        ),
        "encoder": "libx265",
        "preset": "slow",
        "crf": "26",
    },
    {
        "id": "max_h265",
        "label": "Maximale Einsparung (H.265, langsam)",
        "description": (
            "Kleinstmögliche Datei bei noch guter Qualität – deutlich "
            "langsamer als die anderen beiden Profile."
        ),
        "encoder": "libx265",
        "preset": "veryslow",
        "crf": "28",
    },
]

_DEFAULT_PROFILE_ID = "balanced"


def get_profile(profile_id: str) -> dict:
    for profile in OPTIMIZE_PROFILES:
        if profile["id"] == profile_id:
            return profile
    return next(p for p in OPTIMIZE_PROFILES if p["id"] == _DEFAULT_PROFILE_ID)


# ============================================================================
# 2) DAUER ERMITTELN (ohne ffprobe - nur mit dem ohnehin vorhandenen ffmpeg)
# ============================================================================
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration_seconds(path: str, timeout: int = 10) -> float | None:
    """
    Ermittelt die Videodauer in Sekunden - für den Fortschrittsbalken
    (Prozent = bereits verarbeitete Zeit / Gesamtdauer). Nutzt bewusst
    `ffmpeg -i` statt eines separaten ffprobe-Aufrufs, da das Projekt
    ohnehin nur FFmpeg selbst als externe Abhängigkeit voraussetzt.
    Liefert None bei jedem Fehler - der Aufrufer zeigt dann einen
    unbestimmten Fortschritt ("läuft ...") statt eines Prozentwerts an.
    """
    try:
        proc = subprocess.run(
            [get_ffmpeg_path(), "-hide_banner", "-i", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=timeout, **get_subprocess_flags(),
        )
        text = proc.stderr.decode("utf-8", errors="ignore")
        match = _DURATION_RE.search(text)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:
        return None


def probe_media_info(path: str, timeout: int = 10) -> str:
    """
    Liefert die Roh-Ausgabe von `ffmpeg -i <datei>` (Containerkopf samt
    aller Streams).

    Gebraucht fuers Aufnahme-Protokoll: die Gesamtdauer des Containers
    sagt NICHTS darueber aus, ob die Tonspur ueberhaupt vorhanden ist
    oder vorzeitig endet - genau das war der blinde Fleck bei der
    Windows-Diagnose ("Datei volle Laenge, aber Ton nur kurz"). Die
    Stream-Zeilen zeigen Video und Audio dagegen getrennt.
    """
    try:
        proc = subprocess.run(
            [get_ffmpeg_path(), "-hide_banner", "-i", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=timeout, **get_subprocess_flags(),
        )
        text = proc.stderr.decode("utf-8", errors="ignore").strip()
        # "At least one output file must be specified" ist nur die
        # erwartete Beschwerde darueber, dass hier bewusst kein
        # Ausgabeziel angegeben wird - im Protokoll waere sie irrefuehrend.
        lines = [ln for ln in text.splitlines()
                 if "At least one output file" not in ln]
        return "\n".join(lines)
    except Exception as exc:
        return f"(Medieninfo nicht ermittelbar: {exc})"


# ============================================================================
# 3) KOMMANDO-BUILDER
# ============================================================================
def build_optimize_command(input_path: str, output_path: str, profile: dict) -> list:
    """
    Baut das FFmpeg-Kommando für die Nachbearbeitung EINER bereits
    vorhandenen Videodatei.

    Die Tonspur wird bewusst NEU kodiert (AAC, 192 kbit/s) statt per
    Stream-Copy übernommen: Stream-Copy wäre zwar verlustfrei, schlägt
    aber hart fehl, sobald die Quelldatei einen Audio-Codec enthält, der
    im MP4-Container nicht erlaubt ist (z. B. Opus/Vorbis bei Dateien aus
    anderer Quelle) - ein erneutes AAC-Encoding bei 192 kbit/s ist
    dagegen praktisch verlustfrei hörbar und funktioniert immer.

    -progress pipe:1: schreibt maschinenlesbare Fortschrittszeilen
    (u. a. out_time_ms=...) nach STDOUT, getrennt von den eigentlichen
    FFmpeg-Fehlermeldungen auf STDERR - siehe OptimizeThread.run().
    """
    cmd = [
        get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path,
        "-c:v", profile["encoder"],
        "-preset", profile["preset"],
        "-crf", profile["crf"],
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if profile["encoder"] == "libx265":
        cmd += ["-tag:v", "hvc1"]  # QuickTime/Apple-Kompatibilität, wie bei der Aufnahme
    cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-progress", "pipe:1", "-nostats"]
    cmd.append(output_path)
    return cmd


def suggest_output_path(input_path: str) -> str:
    """
    Erzeugt einen Zieldateinamen NEBEN der Originaldatei, der diese
    garantiert nicht überschreibt - auch nicht bei wiederholten Läufen
    (dann wird ' (2)', ' (3)', ... angehängt).
    """
    base, ext = os.path.splitext(input_path)
    candidate = f"{base}_optimiert{ext}"
    counter = 2
    while os.path.exists(candidate):
        candidate = f"{base}_optimiert ({counter}){ext}"
        counter += 1
    return candidate


# ============================================================================
# 4) HINTERGRUND-THREAD
# ============================================================================
class OptimizeThread(threading.Thread):
    """
    Führt die Nachbearbeitung in einem eigenen Thread aus, damit die GUI
    (wie bei RecorderThread/BenchmarkThread) währenddessen nicht einfriert.

    :param on_progress: Callback(percent: float | None) - None, wenn die
                         Gesamtdauer nicht ermittelt werden konnte
                         (dann zeigt die GUI einen unbestimmten Fortschritt).
    :param on_finish:   Callback(output_path: str, original_bytes: int, new_bytes: int)
    :param on_error:    Callback(message: str)
    """

    def __init__(self, input_path: str, output_path: str, profile: dict,
                 on_progress=None, on_finish=None, on_error=None):
        super().__init__(daemon=True)
        self.input_path = input_path
        self.output_path = output_path
        self.profile = profile
        self.on_progress = on_progress
        self.on_finish = on_finish
        self.on_error = on_error

        self._process: subprocess.Popen | None = None
        self._cancelled = threading.Event()
        self._stderr_buffer: list[str] = []

    def cancel(self):
        """Bricht die laufende Optimierung ab und räumt die unfertige Ausgabedatei weg."""
        self._cancelled.set()
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
            except Exception:
                pass

    def _emit(self, callback, *args):
        if callback:
            try:
                callback(*args)
            except Exception:
                pass

    def run(self):
        try:
            duration = probe_duration_seconds(self.input_path)
            cmd = build_optimize_command(self.input_path, self.output_path, self.profile)

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **get_subprocess_flags(),
            )

            # stderr im Hintergrund leeren (wie bei RecorderThread) -
            # verhindert einen Pipe-Deadlock, falls FFmpeg doch mehr als
            # erwartet nach stderr schreibt, während wir stdout lesen.
            threading.Thread(target=self._drain_stderr, daemon=True).start()

            self._read_progress(duration)

            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)

            if self._cancelled.is_set():
                self._cleanup_partial()
                return

            if self._process.returncode == 0 and self._verify_output():
                original_bytes = _safe_size(self.input_path)
                new_bytes = _safe_size(self.output_path)
                self._emit(self.on_progress, 100.0)
                self._emit(self.on_finish, self.output_path, original_bytes, new_bytes)
            else:
                self._cleanup_partial()
                detail = "\n".join(self._stderr_buffer[-10:])
                self._emit(
                    self.on_error,
                    detail or "Optimierung fehlgeschlagen (FFmpeg-Fehler ohne Details).",
                )
        except FileNotFoundError as exc:
            self._cleanup_partial()
            self._emit(self.on_error, str(exc))
        except Exception as exc:
            self._cleanup_partial()
            self._emit(self.on_error, f"Optimierung fehlgeschlagen: {exc}")

    def _read_progress(self, duration: float | None):
        """
        Liest die -progress-Ausgabe von stdout zeilenweise und meldet den
        Fortschritt (falls duration bekannt ist) als Prozentwert 0-100.
        """
        if not self._process or not self._process.stdout:
            return
        for raw in iter(self._process.stdout.readline, b""):
            if self._cancelled.is_set():
                break
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("out_time_ms="):
                continue
            value = line.split("=", 1)[1]
            if value in ("N/A", ""):
                continue
            try:
                # WICHTIG: aus historischen Gründen ist "out_time_ms" bei
                # FFmpeg tatsaechlich in MIKROsekunden (nicht Millisekunden!)
                # - empirisch bestaetigt (out_time_ms=600000 entspricht
                # out_time=00:00:00.6). Durch 1_000_000 statt 1_000 teilen.
                elapsed_seconds = int(value) / 1_000_000
            except ValueError:
                continue
            if duration and duration > 0:
                percent = max(0.0, min(99.0, elapsed_seconds / duration * 100))
                self._emit(self.on_progress, percent)
            else:
                self._emit(self.on_progress, None)

    def _drain_stderr(self):
        try:
            for raw in iter(self._process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="ignore").rstrip()
                if line:
                    self._stderr_buffer.append(line)
                    if len(self._stderr_buffer) > 40:
                        self._stderr_buffer.pop(0)
        except Exception:
            pass

    def _verify_output(self) -> bool:
        try:
            return (os.path.isfile(self.output_path)
                    and os.path.getsize(self.output_path) > 1024)
        except Exception:
            return False

    def _cleanup_partial(self):
        """Entfernt eine unfertige/fehlgeschlagene Ausgabedatei - nie die Originaldatei."""
        try:
            if os.path.isfile(self.output_path):
                os.remove(self.output_path)
        except Exception:
            pass


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0
