"""
diagnosis.py
============
Nachträgliche Prüfung einer fertigen Aufnahme im Hintergrund - GUI-frei.

Früher lief diese Prüfung als nicht abbrechbarer Thread direkt in
gui_main.py. Jetzt ist sie eine eigene Klasse mit Lock und cancel(): beim
Schließen der App beendet cancel() auch die gerade laufende FFmpeg-
Sondierung, statt sie als Kindprozess weiterlaufen zu lassen.

Ergebnisse gehen ausschließlich über Callbacks hinaus:
  log(text)            - Protokolleintrag (Aufnahmeprotokoll)
  warn(art, *details)  - Befund für einen Dialog. Arten:
      "unreadable"                                   Datei nicht auslesbar
      "no_audio"                                     keine Tonspur
      "silent", quelltyp                             Tonspur stumm
      "truncation", erfassung_s, dauer_s             Container zu kurz
      "track_short", erfassung_s, dauer_s, warnungen, abgeschnitten
                                                     Spur endet vorzeitig
                                                     (EIN Dialog, ggf. mit
                                                     Container-Verdacht)
"""

import re
import subprocess
import threading

from optimizer import probe_duration_seconds, probe_max_volume_db, probe_media_info
from recording_validation import check_track_ends

# Lautester Moment der Tonspur unter diesem Wert -> praktisch stumm. Ein
# funktionierendes Mikrofon erreicht selbst in einem stillen Raum durch
# Grundrauschen deutlich hoehere Spitzen; digitale Stille (gesperrter
# Mikrofonzugriff, stummgeschaltetes Geraet) misst -91 dB bzw. -inf.
SILENT_AUDIO_MAX_DB = -70.0

# Unter dieser Erfassungsdauer ist keine verlässliche Aussage möglich.
MIN_DIAGNOSIS_SECONDS = 4.0


# ============================================================================
# REINE HILFSFUNKTIONEN
# ============================================================================
def parse_achieved_fps(media_info: str) -> float | None:
    """
    Liest die TATSÄCHLICH erreichte Bildrate aus der Stream-Zeile.

    FFmpeg gibt dort zwei Werte aus: "9.17 fps" ist der echte
    Durchschnitt (Bilder geteilt durch Dauer), "60 tbr" nur die im
    Container eingetragene Sollrate. Genau diese Verwechslung
    verschleiert das Problem: eine Datei kann "60 tbr" tragen und
    trotzdem nur 9 Bilder pro Sekunde enthalten.
    """
    for line in media_info.splitlines():
        if "Video:" not in line:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s+fps", line)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def fps_log_line(media_info: str, requested_fps: float) -> str | None:
    """
    Protokollzeile "erreichte von angeforderter Bildrate" - bewusst nur fürs
    Protokoll, keine Warnung (ruhiger Bildschirminhalt liefert unter Windows
    von sich aus weniger Bilder, siehe gui_main._check_recording_fps).
    """
    if requested_fps <= 0:
        return None
    achieved = parse_achieved_fps(media_info)
    if achieved is None:
        return None
    ratio = achieved / requested_fps
    note = ""
    if ratio < 0.7:
        note = ("  (deutlich unter dem Sollwert - entweder ruhiger "
                "Bildschirminhalt oder der Encoder kam nicht mit)")
    return (f"Bildrate: {achieved:.1f} von {requested_fps:.0f} fps erreicht "
            f"({ratio * 100:.0f} %){note}")


def silent_audio_hint(source_kind: str, is_windows: bool) -> str:
    """
    Mögliche Ursachen einer stummen Tonspur, abhängig vom Quelltyp
    ('mikrofon', 'systemton', 'unbekannt') und vom Betriebssystem.
    """
    if is_windows:
        if source_kind == "mikrofon":
            return (
                "• Windows sperrt den Mikrofonzugriff: Einstellungen → "
                "Datenschutz und Sicherheit → Mikrofon → \"Desktop-Apps den "
                "Zugriff auf das Mikrofon erlauben\" einschalten.\n"
                "• Das Mikrofon ist stummgeschaltet (Taste am Headset/Notebook "
                "oder in der Sound-Systemsteuerung).\n"
                "• Es ist das falsche Mikrofon gewählt – in der Tonquellen-Liste "
                "das tatsächlich benutzte Gerät auswählen."
            )
        if source_kind == "systemton":
            return (
                "• Während der Aufnahme wurde evtl. nichts abgespielt – die "
                "Systemton-Quelle nimmt nur auf, was der PC gerade wiedergibt.\n"
                "• Die Wiedergabe ist stummgeschaltet oder läuft über ein "
                "anderes Ausgabegerät als das aufgenommene."
            )
        return (
            "• Die gewählte Tonquelle ist stummgeschaltet oder liefert kein Signal.\n"
            "• Es ist evtl. das falsche Gerät gewählt – die Tonquelle im "
            "Video- bzw. Audio-Tab prüfen."
        )

    if source_kind == "mikrofon":
        return (
            "• Das Mikrofon ist stummgeschaltet (pavucontrol / Systemeinstellungen).\n"
            "• Es ist das falsche Gerät gewählt – für den Ton vom PC selbst "
            "die \"Systemton\"-Quelle (Monitor) wählen."
        )
    if source_kind == "systemton":
        return (
            "• Während der Aufnahme wurde evtl. nichts abgespielt – der Monitor "
            "nimmt nur auf, was der PC gerade wiedergibt.\n"
            "• Der Monitor eines anderen Ausgabegeräts ist gewählt oder "
            "stummgeschaltet (pavucontrol → Aufnahme)."
        )
    return (
        "• Die gewählte Tonquelle ist stummgeschaltet oder liefert kein Signal "
        "(pavucontrol / Systemeinstellungen).\n"
        "• Es ist evtl. das falsche Gerät gewählt – die Tonquelle im "
        "Video- bzw. Audio-Tab prüfen."
    )


# ============================================================================
# HINTERGRUND-THREAD
# ============================================================================
class DiagnosisThread(threading.Thread):
    """
    Prüft eine fertige Aufnahme (Dauer, Streams, Bildrate, Stille, Spur-Enden).

    :param path:            Aufnahmedatei
    :param capture_seconds: eingefrorene Erfassungsdauer (RecorderThread)
    :param requested_fps:   angeforderte Bildrate (0 = unbekannt/Ton)
    :param had_audio:       war eine Tonquelle gewählt? (= Tonspur erwartet)
    :param audio_only:      reine Tonaufnahme? (dann keine Bildspur erwartet)
    :param audio_offset_ms: eingestellter Ton-Versatz
    :param source_kind:     'mikrofon' / 'systemton' / 'unbekannt'
    :param log:             Callback(text)
    :param warn:            Callback(art, *details) - siehe Moduldocstring
    :param ffmpeg:          FFmpeg-Pfad (None = get_ffmpeg_path())
    """

    def __init__(self, path: str, *, capture_seconds: float, requested_fps: float = 0.0,
                 had_audio: bool = False, audio_only: bool = False,
                 audio_offset_ms: int = 0, source_kind: str = "unbekannt",
                 log=None, warn=None, ffmpeg: str | None = None):
        super().__init__(daemon=True)
        self.path = path
        self.capture_seconds = float(capture_seconds or 0.0)
        self.requested_fps = float(requested_fps or 0.0)
        self.had_audio = bool(had_audio)
        self.audio_only = bool(audio_only)
        self.audio_offset_ms = 0 if audio_only else int(audio_offset_ms or 0)
        self.source_kind = source_kind
        self._log_cb = log
        self._warn_cb = warn
        self._ffmpeg = ffmpeg

        self._process: subprocess.Popen | None = None
        self._cancelled = threading.Event()
        # Schützt Abbruch-Flag und Prozessveröffentlichung
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def cancel(self):
        """Bricht die Prüfung ab und beendet eine laufende FFmpeg-Sondierung."""
        with self._lock:
            self._cancelled.set()
            process = self._process
        _kill_quietly(process)

    def kill(self):
        """Hart beenden (nach Ablauf des Wartebudgets beim Schließen)."""
        self.cancel()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _publish_process(self, process):
        """Veröffentlicht einen Sondierungsprozess und prüft danach erneut auf Abbruch."""
        with self._lock:
            self._process = process
            cancelled = self._cancelled.is_set()
        if cancelled:
            _kill_quietly(process)

    def _log(self, text: str):
        if self._log_cb is not None and not self.cancelled:
            try:
                self._log_cb(text)
            except Exception:
                pass

    def _warn(self, kind: str, *details):
        if self._warn_cb is not None and not self.cancelled:
            try:
                self._warn_cb(kind, *details)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def run(self):
        try:
            self._run()
        except Exception as exc:
            self._log(f"Diagnose abgebrochen: {exc}")

    def _run(self):
        capture = self.capture_seconds
        if self.cancelled or capture < MIN_DIAGNOSIS_SECONDS:
            return  # zu kurz fuer eine verlaessliche Aussage

        duration = probe_duration_seconds(self.path, on_process=self._publish_process)
        if self.cancelled:
            return
        if duration is None:
            # Die Dauer liess sich nicht einmal auslesen - genau der Fall
            # "Datei laesst sich gar nicht abspielen".
            self._log(
                f"BEFUND: Aufnahme lief {capture:.0f}s, aber die Dauer der "
                "Datei ist nicht auslesbar - Datei vermutlich beschaedigt/unvollstaendig."
            )
            self._warn("unreadable")
            return

        self._log(f"Dauer laut Datei: {duration:.1f}s (Erfassungsdauer: {capture:.1f}s)")

        # Streams der fertigen Datei protokollieren. Die Containerdauer
        # allein verraet NICHT, ob die Tonspur vorhanden ist oder
        # vorzeitig endet.
        info = probe_media_info(self.path, on_process=self._publish_process)
        if self.cancelled:
            return
        self._log("Inhalt der Datei laut FFmpeg:\n" + info)

        fps_line = fps_log_line(info, self.requested_fps)
        if fps_line:
            self._log(fps_line)

        if "Audio:" not in info:
            if self.had_audio:
                self._log(
                    "BEFUND: Es war eine Tonquelle ausgewaehlt, die Datei enthaelt "
                    "aber ueberhaupt keinen Audiostream."
                )
                self._warn("no_audio")
        elif self.had_audio:
            max_db = probe_max_volume_db(self.path, on_process=self._publish_process)
            if self.cancelled:
                return
            if max_db is not None:
                self._log(f"Tonpegel: lautester Moment {max_db:.1f} dB")
                if max_db <= SILENT_AUDIO_MAX_DB:
                    self._log(
                        "BEFUND: Die Tonspur ist vorhanden, enthaelt aber nur "
                        "Stille (Zugriff gesperrt, Geraet stumm, nichts "
                        "abgespielt oder falsches Geraet gewaehlt)."
                    )
                    self._warn("silent", self.source_kind)

        truncated = duration < capture * 0.7 - 1.0
        if truncated:
            self._log(
                f"BEFUND: Datei deutlich kuerzer als die Erfassungsdauer "
                f"({duration:.0f}s statt {capture:.0f}s) - vorzeitig abgeschnitten."
            )

        track_warnings = check_track_ends(
            self.path, self._ffmpeg_path(),
            expect_video=not self.audio_only,
            expect_audio=self.had_audio,
            capture_seconds=capture,
            audio_offset_ms=self.audio_offset_ms,
            on_process=self._publish_process,
            cancelled=lambda: self.cancelled,
            log=self._log,
        )
        if self.cancelled:
            return
        for warning in track_warnings:
            self._log("BEFUND: " + warning)

        if track_warnings:
            # Kurze Spur und ggf. Container-Verdacht in EINEM Dialog.
            self._warn("track_short", capture, duration, list(track_warnings), truncated)
        elif truncated:
            self._warn("truncation", capture, duration)

    def _ffmpeg_path(self) -> str:
        if self._ffmpeg:
            return self._ffmpeg
        from ffmpeg_utils import get_ffmpeg_path
        return get_ffmpeg_path()


def _kill_quietly(process):
    if process is None:
        return
    try:
        if process.poll() is None:
            process.kill()
    except Exception:
        pass
