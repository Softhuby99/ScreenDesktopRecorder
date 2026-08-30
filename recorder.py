"""
recorder.py
===========
Steuert den FFmpeg-Aufnahmeprozess in einem separaten Thread.

Kernpunkte:
  * Sauberes Beenden über stdin-'q' -> FFmpeg schreibt den MOOV-Atom
    korrekt in die MP4-Datei (kein Datenverlust / keine kaputte Datei).
  * Pause/Resume über Prozess-Suspend (psutil.suspend/resume).
    Funktioniert auf beiden Plattformen:
      Linux   -> SIGSTOP / SIGCONT
      Windows -> SuspendThread über psutil
  * stderr wird kontinuierlich geleert -> kein Pipe-Deadlock bei langen
    Aufnahmen (FFmpeg schreibt permanent Fortschrittszeilen!).
  * Timer-Logik rechnet Pausenzeiten korrekt heraus.
"""

import os
import subprocess
import threading
import time

import psutil

from ffmpeg_utils import build_record_command
from platform_utils import get_subprocess_flags


class RecorderThread(threading.Thread):
    """
    Worker-Thread, der den FFmpeg-Prozess startet und überwacht.

    :param settings: dict mit allen Aufnahmeparametern
    :param on_started: Callback()
    :param on_stopped: Callback(output_path: str, success: bool)
    :param on_error:   Callback(message: str)
    """

    def __init__(self, settings: dict, on_started=None, on_stopped=None, on_error=None):
        super().__init__(daemon=True)
        self.settings = settings
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.on_error = on_error

        self._process: subprocess.Popen | None = None
        self._ps_process: psutil.Process | None = None

        self._stop_event = threading.Event()
        self._is_paused = False
        self._lock = threading.Lock()

        # Zeitmessung
        self._start_time: float = 0.0
        self._paused_total: float = 0.0
        self._pause_started: float = 0.0

        # Öffentlicher Zustand
        self.output_path: str = settings.get("output_path", "")
        self.error_message: str = ""

        # Ringpuffer der letzten stderr-Zeilen (für Fehlermeldungen)
        self._stderr_buffer: list[str] = []

    # ==================================================================
    # ÖFFENTLICHE API (wird vom GUI-Thread aufgerufen)
    # ==================================================================
    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def is_active(self) -> bool:
        """True, solange der FFmpeg-Prozess läuft."""
        return self._process is not None and self._process.poll() is None

    def get_elapsed(self) -> float:
        """Effektive Aufnahmedauer in Sekunden (Pausen herausgerechnet)."""
        if self._start_time == 0.0:
            return 0.0
        now = time.time()
        paused = self._paused_total
        if self._is_paused and self._pause_started:
            paused += now - self._pause_started
        return max(0.0, now - self._start_time - paused)

    def toggle_pause(self) -> bool:
        """
        Pausiert bzw. setzt die Aufnahme fort.
        :return: neuer Pausenzustand (True = pausiert)
        """
        with self._lock:
            if not self.is_active or not self._ps_process:
                return self._is_paused

            try:
                if self._is_paused:
                    # ---- FORTSETZEN (SIGCONT / ResumeThread) ----
                    self._ps_process.resume()
                    self._paused_total += time.time() - self._pause_started
                    self._pause_started = 0.0
                    self._is_paused = False
                else:
                    # ---- PAUSIEREN (SIGSTOP / SuspendThread) ----
                    self._ps_process.suspend()
                    self._pause_started = time.time()
                    self._is_paused = True
            except Exception:
                pass

            return self._is_paused

    def stop(self):
        """Signalisiert dem Thread, die Aufnahme sauber zu beenden."""
        self._stop_event.set()

    # ==================================================================
    # THREAD-HAUPTSCHLEIFE  (nur EINMAL definiert!)
    # ==================================================================
    def run(self):
        try:
            cmd = build_record_command(
                output_path=self.settings["output_path"],
                fps=self.settings["fps"],
                encoder=self.settings["encoder"],
                preset=self.settings["preset"],
                mode_region=self.settings.get("mode_region", False),
                region=self.settings.get("region"),
                audio_device=self.settings.get("audio_device"),
                audio_only=self.settings.get("audio_only", False),
                gain=self.settings.get("gain", 1.0),
                denoise=self.settings.get("denoise", False),
            )

            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **get_subprocess_flags(),
            )

            # Kurz warten: startet FFmpeg überhaupt? (falsches Gerät, X11-Fehler ...)
            time.sleep(0.8)
            if self._process.poll() is not None:
                self.error_message = self._read_stderr_tail() or \
                    "FFmpeg konnte die Aufnahme nicht starten."
                self._emit(self.on_error, self.error_message)
                self._emit(self.on_stopped, self.output_path, False)
                return

            # stderr im Hintergrund leeren -> verhindert Pipe-Deadlock
            self._stderr_buffer.clear()
            threading.Thread(target=self._drain_stderr, daemon=True).start()

            # psutil-Handle für Pause/Resume
            try:
                self._ps_process = psutil.Process(self._process.pid)
            except Exception:
                self._ps_process = None

            self._start_time = time.time()
            self._emit(self.on_started)

            # ---- Überwachungsschleife (sehr sparsam: 0.25 s Takt) ----
            while not self._stop_event.is_set():
                if self._process.poll() is not None:
                    break                       # FFmpeg vorzeitig beendet
                self._stop_event.wait(0.25)

            # ---- Unerwarteter Absturz? (Platte voll, Encoder-Fehler ...) ----
            exit_code = self._process.poll()
            if not self._stop_event.is_set() and exit_code not in (None, 0):
                self.error_message = "\n".join(self._stderr_buffer[-10:]) or \
                    f"FFmpeg wurde unerwartet beendet (Code {exit_code})."
                self._emit(self.on_error, self.error_message)

            # ---- Sauberes Beenden ----
            success = self._graceful_stop()
            self._emit(self.on_stopped, self.output_path, success)

        except FileNotFoundError as exc:
            self.error_message = str(exc)
            self._emit(self.on_error, self.error_message)
            self._emit(self.on_stopped, self.output_path, False)
        except Exception as exc:
            self.error_message = f"Aufnahmefehler: {exc}"
            self._emit(self.on_error, self.error_message)
            self._emit(self.on_stopped, self.output_path, False)

    # ==================================================================
    # INTERNE HILFSMETHODEN
    # ==================================================================
    def _emit(self, callback, *args):
        """Ruft ein Callback fehlertolerant auf."""
        if callback:
            try:
                callback(*args)
            except Exception:
                pass

    def _read_stderr_tail(self, max_chars: int = 800) -> str:
        """Liest stderr eines bereits beendeten Prozesses (nur beim Startfehler)."""
        try:
            data = self._process.stderr.read() or b""
            text = data.decode("utf-8", errors="ignore").strip()
            return text[-max_chars:]
        except Exception:
            return ""

    def _drain_stderr(self):
        """
        Liest stderr kontinuierlich leer.

        WICHTIG: FFmpeg schreibt im Sekundentakt Fortschrittszeilen nach
        stderr. Wird die Pipe nicht geleert, läuft der OS-Puffer (~64 KB)
        voll und FFmpeg blockiert -> die Aufnahme friert ein.
        Die letzten 40 Zeilen werden für Fehlermeldungen aufbewahrt.
        """
        try:
            for raw in iter(self._process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="ignore").rstrip()
                if line:
                    self._stderr_buffer.append(line)
                    if len(self._stderr_buffer) > 40:
                        self._stderr_buffer.pop(0)
        except Exception:
            pass

    def _graceful_stop(self) -> bool:
        """
        Beendet FFmpeg datenverlustfrei.

        WICHTIG: Ein pausierter (suspendierter) Prozess kann kein 'q'
        verarbeiten -> vorher IMMER resume() aufrufen.
        """
        if not self._process:
            return False

        # 1) Pause aufheben, sonst hängt der Stop
        if self._is_paused and self._ps_process:
            try:
                self._ps_process.resume()
                self._paused_total += time.time() - self._pause_started
                self._pause_started = 0.0
                self._is_paused = False
            except Exception:
                pass

        if self._process.poll() is not None:
            return self._verify_output()

        # 2) 'q' an stdin -> FFmpeg finalisiert den MOOV-Atom der MP4
        try:
            if self._process.stdin:
                self._process.stdin.write(b"q")
                self._process.stdin.flush()
                self._process.stdin.close()
        except Exception:
            pass

        # 3) Auf sauberen Abschluss warten (großzügig für alte CPUs)
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                self._process.terminate()       # SIGTERM / TerminateProcess
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()        # letzte Instanz
                    self._process.wait(timeout=3)
                except Exception:
                    pass

        return self._verify_output()

    def _verify_output(self) -> bool:
        """Prüft, ob eine verwertbare Datei entstanden ist (> 1 KB)."""
        try:
            return (os.path.isfile(self.output_path)
                    and os.path.getsize(self.output_path) > 1024)
        except Exception:
            return False