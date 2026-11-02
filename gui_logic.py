"""
gui_logic.py
============
GUI-freie Entscheidungslogik des Hauptfensters (gui_main.MainWindow).

Liegt bewusst außerhalb von gui_main.py, damit sie ohne Display, ohne
customtkinter und ohne Audiohardware testbar ist (tests/test_gui_closing.py).
"""

import os
import sys
import time

from audio_devices import looks_like_system_audio

# Präfix für Ausgaben auf stderr beim Schließen/Abbrechen (es gibt kein App-Log).
STDERR_PREFIX = "[ScreenRecPro] "

# Gemeinsames Wartebudget für Optimierung, Leistungstest und Diagnose beim
# Schließen. Der Recorder hat KEIN eigenes Limit darunter (siehe recorder.py).
WORKER_SHUTDOWN_BUDGET_SECONDS = 10.0
# Startphase des Recorders (ddagrab-Prüfung hat selbst 10 s Timeout) + Reserve
RECORDER_STARTUP_WAIT_SECONDS = 12.0

# Aufträge, die _post_to_gui im Zustand "schließt" noch ausführt.
POST_ALLOWED_WHILE_CLOSING = frozenset({
    "_write_recording_log",
    "_append_recording_log",
    "_set_status",
})

CLOSE_SAVE = "speichern"
CLOSE_DISCARD = "verwerfen"
CLOSE_CANCEL = "abbrechen"

SOURCE_SYSTEM = "systemton"
SOURCE_MIC = "mikrofon"
SOURCE_UNKNOWN = "unbekannt"


def log_stderr(text: str) -> None:
    """Protokollzeile auf stderr mit dem Präfix [ScreenRecPro]."""
    try:
        print(STDERR_PREFIX + text, file=sys.stderr, flush=True)
    except Exception:
        pass


def recording_running(rec) -> bool:
    """
    "Aufnahme läuft" = es gibt einen lebenden RecorderThread.

    Bewusst NICHT rec.is_active: das wird erst wahr, wenn der FFmpeg-Prozess
    steht. Davor läuft der Thread schon (z. B. ddagrab-Prüfung, bis ca. 10 s)
    - auch in dieser Startphase muss die Rückfrage beim Schließen kommen.
    """
    if rec is None:
        return False
    try:
        return bool(rec.is_alive())
    except Exception:
        return False


def close_choice(answer) -> str:
    """Rückgabe von messagebox.askyesnocancel -> Entscheidung beim Schließen."""
    if answer is True:
        return CLOSE_SAVE
    if answer is False:
        return CLOSE_DISCARD
    return CLOSE_CANCEL       # None: Abbrechen, Esc oder X des Dialogs


def post_allowed(closing: bool, fn) -> bool:
    """Darf ein Auftrag aus einem Hintergrund-Thread noch ausgeführt werden?"""
    if not closing:
        return True
    return getattr(fn, "__name__", "") in POST_ALLOWED_WHILE_CLOSING


def remove_partial_output(path: str | None) -> bool:
    """
    Löscht eine unfertige Ausgabedatei (Nein-Pfad beim Schließen).
    Fehlt die Datei schon, ist das kein Fehler; andere OSError werden auf
    stderr protokolliert und nicht weitergereicht.
    :return: True, wenn danach keine Datei mehr existiert
    """
    if not path:
        return True
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_stderr(f"Unfertige Aufnahme konnte nicht gelöscht werden: {path} ({exc})")
        return False
    return True


def recording_source_kind(label: str | None, audio_map: dict) -> str:
    """
    Quelltyp der Aufnahme für Diagnose und Stille-Hinweis:
    'systemton', wenn der Name nach Systemton/Loopback aussieht;
    'mikrofon', wenn nicht, das Label nicht leer und eine bekannte Quelle ist;
    sonst 'unbekannt'.
    """
    label = label or ""
    if label and looks_like_system_audio(label):
        return SOURCE_SYSTEM
    if label and label in (audio_map or {}):
        return SOURCE_MIC
    return SOURCE_UNKNOWN


def wait_for_workers(workers, budget_seconds: float = WORKER_SHUTDOWN_BUDGET_SECONDS,
                     pump=None, clock=time.monotonic, step: float = 0.1) -> list:
    """
    Bricht Worker (Optimierung, Leistungstest, Diagnose) ab und wartet
    pumpend höchstens budget_seconds INSGESAMT. Wer danach noch lebt, wird
    hart beendet (kill() bzw. cancel()) und auf stderr gemeldet.

    :param pump: wird zwischen den Warteschritten aufgerufen (z. B. Tk-update)
    :return: Liste der Worker, die das Budget überschritten haben
    """
    workers = [w for w in workers if w is not None]
    for worker in workers:
        try:
            worker.cancel()
        except Exception:
            pass

    deadline = clock() + budget_seconds
    alive = []
    while True:
        alive = [w for w in workers if _is_alive(w)]
        if not alive:
            return []
        if clock() >= deadline:
            break
        for worker in alive:
            try:
                worker.join(timeout=step)
            except Exception:
                pass
        if pump is not None:
            try:
                pump()
            except Exception:
                pass

    for worker in alive:
        log_stderr(f"{type(worker).__name__} nach {budget_seconds:.0f} s nicht beendet "
                   "- wird hart beendet.")
        try:
            getattr(worker, "kill", worker.cancel)()
        except Exception:
            pass
    return alive


def wait_for_recorder_startup(rec, budget_seconds: float = RECORDER_STARTUP_WAIT_SECONDS,
                              pump=None, clock=time.monotonic, step: float = 0.1) -> bool:
    """
    Nein-Pfad in der Startphase (SRP1-IR-02): Der RecorderThread führt noch
    seine Vorprüfung aus (ddagrab, eigener FFmpeg-Kindprozess), force_kill()
    kennt diesen Prozess nicht. Nach stop() beendet der Thread die Vorprüfung
    und startet nichts mehr - hier wird (pumpend) darauf gewartet, höchstens
    budget_seconds. Ohne Startphase kehrt die Funktion sofort zurück.

    :return: True, wenn nichts mehr zu warten war bzw. der Thread beendet ist
    """
    if not getattr(rec, "in_startup", False):
        return True
    deadline = clock() + budget_seconds
    while _is_alive(rec):
        if clock() >= deadline:
            log_stderr(f"Aufnahme-Startphase nach {budget_seconds:.0f} s nicht beendet "
                       "- Fenster wird trotzdem geschlossen.")
            return False
        try:
            rec.join(timeout=step)
        except Exception:
            pass
        if pump is not None:
            try:
                pump()
            except Exception:
                pass
    return True


def _is_alive(worker) -> bool:
    try:
        return bool(worker.is_alive())
    except Exception:
        return False
