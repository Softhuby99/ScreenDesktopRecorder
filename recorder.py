"""
recorder.py
===========
Steuert den FFmpeg-Aufnahmeprozess in einem separaten Thread.

Kernpunkte:
  * Sauberes Beenden über stdin-'q' -> FFmpeg schreibt den MOOV-Atom
    korrekt in die MP4-Datei (kein Datenverlust / keine kaputte Datei).
    Danach wird gewartet, solange FFmpeg noch arbeitet - nicht nur eine
    feste Zeit (siehe _wait_while_finalizing).
  * Pause/Resume über Prozess-Suspend (psutil.suspend/resume).
    Funktioniert auf beiden Plattformen:
      Linux   -> SIGSTOP / SIGCONT
      Windows -> SuspendThread über psutil
  * stderr wird kontinuierlich geleert -> kein Pipe-Deadlock bei langen
    Aufnahmen (FFmpeg schreibt permanent Fortschrittszeilen!).
  * Timer-Logik rechnet Pausenzeiten korrekt heraus.
  * Abbruch-Flag und Prozessreferenz stehen unter EINEM Lock: ein stop()
    gleichzeitig zum Start startet entweder gar keinen Prozess oder
    finalisiert den gerade gestarteten sauber per 'q'.
  * Die Erfassungsdauer (aktive Aufnahmezeit ohne Pausen) wird beim Ende
    der Überwachung eingefroren - die Finalisierung zählt nicht mit.
"""

import subprocess
import threading
import time

import psutil

from ffmpeg_utils import build_record_command, get_ffmpeg_path
from recording_validation import validate_recording
from platform_utils import get_subprocess_flags

# Abschluss nach dem Stoppen (siehe RecorderThread._wait_while_finalizing)
FINALIZE_IDLE_SECONDS = 15     # so lange ohne jede Aktivitaet -> gilt als haengend
FINALIZE_BLIND_SECONDS = 60    # Obergrenze, falls psutil keine Messwerte liefert
FINALIZE_MAX_SECONDS = 900     # absolute Obergrenze (15 min)

# Wartezeit nach dem Prozessstart: startet FFmpeg überhaupt?
START_CHECK_SECONDS = 0.8


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

        # Das tatsächlich ausgeführte FFmpeg-Kommando - wird für das
        # Diagnose-Log gebraucht (siehe gui_main._write_recording_log).
        # Ohne das Kommando laesst sich an einem Fehlerbild kaum
        # nachvollziehen, mit welchen Parametern aufgenommen wurde.
        self.command: list[str] = []
        self._stderr_thread = None
        self._capture_failure = ""

        # Uhr (in Tests austauschbar)
        self._clock = time.monotonic

        # Eingefrorene Erfassungsdauer (siehe _freeze_capture_seconds)
        self._capture_seconds: float = 0.0
        self._capture_frozen = False

        # Ergebnis von run(): None = noch nicht beendet
        self.success: bool | None = None

    # ==================================================================
    # ÖFFENTLICHE API (wird vom GUI-Thread aufgerufen)
    # ==================================================================
    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def in_startup(self) -> bool:
        """
        True, solange der Thread lebt, aber noch kein FFmpeg-Prozess
        veröffentlicht ist (Startphase, z. B. ddagrab-Prüfung). In dieser
        Phase gibt es einen Kindprozess, den force_kill() nicht kennt.
        """
        with self._lock:
            return self.is_alive() and self._process is None

    @property
    def is_active(self) -> bool:
        """True, solange der FFmpeg-Prozess läuft."""
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    def _active_seconds(self) -> float:
        """Aktive Aufnahmezeit bis jetzt (Pausen abgezogen). Aufrufer hält ggf. den Lock."""
        if self._start_time == 0.0:
            return 0.0
        now = self._clock()
        paused = self._paused_total
        if self._is_paused and self._pause_started:
            paused += now - self._pause_started
        return max(0.0, now - self._start_time - paused)

    def get_elapsed(self) -> float:
        """Effektive Aufnahmedauer in Sekunden (Pausen herausgerechnet)."""
        if self._capture_frozen:
            return self._capture_seconds
        return self._active_seconds()

    def get_capture_seconds(self) -> float:
        """
        Eingefrorene Erfassungsdauer in Sekunden (immer float). 0.0, solange
        nicht eingefroren oder wenn die Aufnahme nie richtig lief. Protokoll
        und Spur-Endeprüfung verwenden ausschließlich diesen Wert - die
        Finalisierungszeit nach dem Stoppen zählt nicht mit.
        """
        return float(self._capture_seconds)

    def _freeze_capture_seconds(self):
        """Friert die Erfassungsdauer ein (idempotent, erster Wert gewinnt)."""
        with self._lock:
            self._freeze_capture_seconds_locked()

    def _freeze_capture_seconds_locked(self):
        if self._capture_frozen:
            return
        try:
            self._capture_seconds = float(self._active_seconds())
        except Exception:
            self._capture_seconds = 0.0
        self._capture_frozen = True

    def _resume_locked(self) -> bool:
        """
        Hebt eine Pause auf. NUR mit gehaltenem self._lock aufrufen.
        :return: True, wenn tatsächlich fortgesetzt wurde
        """
        if not self._is_paused or not self._ps_process:
            return False
        try:
            # ---- FORTSETZEN (SIGCONT / ResumeThread) ----
            self._ps_process.resume()
        except Exception:
            return False
        self._paused_total += self._clock() - self._pause_started
        self._pause_started = 0.0
        self._is_paused = False
        return True

    def toggle_pause(self) -> bool:
        """
        Pausiert bzw. setzt die Aufnahme fort.

        Nach stop() bzw. nach dem Einfrieren der Erfassungsdauer gesperrt -
        dann wird nur der bisherige Zustand zurückgegeben.
        :return: neuer Pausenzustand (True = pausiert)
        """
        with self._lock:
            if self._stop_event.is_set() or self._capture_frozen:
                return self._is_paused
            process = self._process
            if process is None or process.poll() is not None or not self._ps_process:
                return self._is_paused

            if self._is_paused:
                self._resume_locked()
            else:
                try:
                    # ---- PAUSIEREN (SIGSTOP / SuspendThread) ----
                    self._ps_process.suspend()
                    self._pause_started = self._clock()
                    self._is_paused = True
                except Exception:
                    pass

            return self._is_paused

    def stop(self):
        """
        Signalisiert dem Thread, die Aufnahme sauber zu beenden. Wirkt in
        jedem Zustand (auch vor oder während des FFmpeg-Starts) und ist
        idempotent. Friert die Erfassungsdauer ein, falls die Aufnahme
        schon lief.
        """
        with self._lock:
            self._stop_event.set()
            if self._start_time != 0.0:
                self._freeze_capture_seconds_locked()

    def force_kill(self):
        """
        LETZTE INSTANZ, ausschliesslich fuer den Fall, dass der GUI-Thread
        beim Beenden der App nicht mehr auf die vollstaendige _graceful_stop()
        warten kann/will (siehe gui_main._on_close): toetet den FFmpeg-Prozess
        SOFORT, ohne auf das MOOV-Atom-Finalisieren zu warten - die Datei
        kann danach beschaedigt/unvollstaendig sein. Sicher von einem
        ANDEREN Thread aus aufrufbar, auch waehrend _graceful_stop() selbst
        noch laeuft (Popen.kill()/.wait() auf einem bereits beendeten
        Prozess ist ein no-op, keine Race-Condition).
        """
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
            # Nur das Einsammeln des getöteten Prozesses abwarten (kurz,
            # damit die Datei z. B. unter Windows danach löschbar ist) -
            # das ist keine Finalisierungswartezeit.
            try:
                process.wait(timeout=2)
            except Exception:
                pass

    # ==================================================================
    # THREAD-HAUPTSCHLEIFE  (nur EINMAL definiert!)
    # ==================================================================
    def run(self):
        try:
            self._run()
        finally:
            # Ausnahmepfade: Erfassungsdauer trotzdem setzen (dann 0.0 bzw.
            # der bis dahin erreichte Wert) - idempotent, erster Wert gewinnt.
            self._freeze_capture_seconds()

    def _start_process(self, cmd: list):
        """
        Startet FFmpeg nur, wenn noch kein Abbruch angefordert ist, und
        veröffentlicht die Prozessreferenz unter demselben Lock.
        :return: Popen oder None (Abbruch vor dem Start)
        """
        with self._lock:
            if self._stop_event.is_set():
                return None
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **get_subprocess_flags(),
            )
            return self._process

    def _finish(self, success: bool):
        # Vor dem Callback einfrieren, damit der Empfänger immer einen
        # festen Wert sieht (Ausnahmepfade: 0.0).
        self._freeze_capture_seconds()
        self.success = success
        self._emit(self.on_stopped, self.output_path, success)

    def _run(self):
        try:
            if self._stop_event.is_set():
                # Abbruch vor dem Start: nichts starten.
                self._finish(False)
                return
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
                # Vom GUI-Thread VOR dem Threadstart ermittelt (siehe
                # gui_main._start_recording) - build_video_input_args()
                # darf get_screen_size() nicht selbst aufrufen, das würde
                # aus diesem Worker-Thread heraus ein zweites Tk-Root
                # erzeugen (siehe platform_utils.get_screen_size-Docstring).
                screen_size=self.settings.get("screen_size"),
                audio_offset_ms=self.settings.get("audio_offset_ms", 0),
            )
            self.command = list(cmd)

            process = self._start_process(cmd)
            if process is None:
                # Abbruch während der Startphase (z. B. ddagrab-Prüfung):
                # kein Prozess gestartet.
                self._finish(False)
                return

            # Ab Prozessstart leeren, auch waehrend der Initialisierung.
            self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self._stderr_thread.start()

            # psutil-Handle für Pause/Resume
            try:
                ps_process = psutil.Process(process.pid)
            except Exception:
                ps_process = None
            with self._lock:
                self._ps_process = ps_process

            if self._stop_event.is_set():
                # Abbruch zwischen Prüfung und Veröffentlichung: den gerade
                # gestarteten Prozess sofort per 'q' finalisieren und einsammeln.
                self._freeze_capture_seconds()
                self._finish(self._graceful_stop())
                return

            # Kurz warten: startet FFmpeg überhaupt? (falsches Gerät, X11-Fehler ...)
            self._stop_event.wait(START_CHECK_SECONDS)
            if process.poll() is not None:
                self._stderr_thread.join(timeout=2)
                self.error_message = "\n".join(self._stderr_buffer[-10:]) or \
                    "FFmpeg konnte die Aufnahme nicht starten."
                self._emit(self.on_error, self.error_message)
                self._finish(False)
                return

            with self._lock:
                self._start_time = self._clock()
            if not self._stop_event.is_set():
                self._emit(self.on_started)

            # ---- Überwachungsschleife (sehr sparsam: 0.25 s Takt) ----
            while not self._stop_event.is_set():
                if process.poll() is not None:
                    break                       # FFmpeg vorzeitig beendet
                self._stop_event.wait(0.25)

            # Erfassungsdauer einfrieren, BEVOR die Finalisierung beginnt.
            self._freeze_capture_seconds()

            # ---- Unerwarteter Absturz? (Platte voll, Encoder-Fehler ...) ----
            exit_code = process.poll()
            if not self._stop_event.is_set() and exit_code not in (None, 0):
                self.error_message = "\n".join(self._stderr_buffer[-10:]) or \
                    f"FFmpeg wurde unerwartet beendet (Code {exit_code})."
                self._emit(self.on_error, self.error_message)

            # ---- Sauberes Beenden ----
            success = self._graceful_stop()
            if not success and self.error_message and exit_code in (None, 0):
                self._emit(self.on_error, self.error_message)
            self._finish(success)

        except FileNotFoundError as exc:
            self.error_message = str(exc)
            self._emit(self.on_error, self.error_message)
            self._finish(False)
        except Exception as exc:
            self.error_message = f"Aufnahmefehler: {exc}"
            self._emit(self.on_error, self.error_message)
            self._finish(False)

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
                    if any(marker in line.lower() for marker in (
                        "frame duplication too large", "no filtered frames for output stream",
                        "nothing was encoded", "conversion failed",
                    )):
                        self._capture_failure = line
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
        with self._lock:
            process = self._process
            # 1) Pause aufheben, sonst hängt der Stop (unter dem Lock mit
            #    Zustandsprüfung - ein paralleles toggle_pause() kann
            #    nicht zusätzlich fortsetzen, siehe toggle_pause)
            self._resume_locked()
        if not process:
            return False

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

        # 3) Auf sauberen Abschluss warten - solange FFmpeg noch arbeitet
        if not self._wait_while_finalizing():
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

    def _wait_while_finalizing(self) -> bool:
        """
        Wartet auf das Ende von FFmpeg nach dem 'q' - so lange, wie FFmpeg
        noch erkennbar arbeitet (CPU-Zeit oder Lese-/Schreibzugriffe steigen).

        Früher wurde nach festen 10 Sekunden hart beendet. Das reicht bei
        langen Aufnahmen nicht: wegen '-movflags +faststart' schreibt
        FFmpeg beim Abschluss die GESAMTE Datei noch einmal um (der
        Inhaltsverzeichnis-Block wandert an den Anfang), dazu kodiert der
        Encoder die noch gepufferten Bilder fertig. Bei mehreren GB auf
        einer langsamen Platte dauert das deutlich länger - ein hartes
        Beenden mittendrin hinterlässt eine unbrauchbare MP4.

        Hart beendet wird nur noch, wenn sich FINALIZE_IDLE_SECONDS lang
        gar nichts mehr tut (FFmpeg hängt) oder FINALIZE_MAX_SECONDS
        erreicht sind.

        :return: True, wenn FFmpeg von selbst beendet wurde
        """
        started = self._clock()
        last_activity = started
        last_snapshot = self._activity_snapshot()
        # Ohne psutil-Messwerte laesst sich "arbeitet noch" nicht beurteilen -
        # dann gilt eine feste, grosszuegige Obergrenze.
        idle_limit = FINALIZE_IDLE_SECONDS if last_snapshot is not None else FINALIZE_BLIND_SECONDS

        while True:
            try:
                self._process.wait(timeout=1.0)
                return True
            except subprocess.TimeoutExpired:
                pass

            now = self._clock()
            snapshot = self._activity_snapshot()
            if snapshot is not None and snapshot != last_snapshot:
                last_activity = now
                last_snapshot = snapshot

            if now - last_activity > idle_limit or now - started > FINALIZE_MAX_SECONDS:
                return False

    def _activity_snapshot(self):
        """
        (CPU-Zeit, gelesene Bytes, geschriebene Bytes) des FFmpeg-Prozesses -
        ändert sich der Wert, arbeitet FFmpeg noch. None, wenn psutil die
        Werte nicht liefern kann.
        """
        if not self._ps_process:
            return None
        try:
            cpu = self._ps_process.cpu_times()
            cpu_total = round(cpu.user + cpu.system, 2)
        except Exception:
            return None
        try:
            io = self._ps_process.io_counters()
            return (cpu_total, io.read_bytes, io.write_bytes)
        except Exception:
            return (cpu_total, 0, 0)

    def _verify_output(self) -> bool:
        """Pflichtspuren pruefen; Exitcode 0 und Dateigroesse genuegen nicht."""
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)
        if self._capture_failure:
            self.error_message = "Aufnahmefehler: " + self._capture_failure
        elif self._process and self._process.returncode != 0:
            self.error_message = f"FFmpeg wurde mit Code {self._process.returncode} beendet."
        else:
            self.error_message = validate_recording(
                self.output_path, get_ffmpeg_path(),
                expect_video=not self.settings.get("audio_only", False),
                expect_audio=bool(self.settings.get("audio_device")) or self.settings.get("audio_only", False),
            )
        if self.error_message:
            self._stderr_buffer.append("PRUEFUNG: " + self.error_message)
            return False
        return True
