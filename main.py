#!/usr/bin/env python3
"""
main.py
=======
Einstiegspunkt von ScreenRec Pro.

Aufgaben:
  1. Abhängigkeiten prüfen und verständliche Fehlermeldungen ausgeben
  2. DPI-Awareness setzen (Windows) bzw. Display prüfen (Linux)
  3. Hauptfenster starten

Start:
    python main.py
"""

import sys
import os


# ============================================================================
# 1) ABHÄNGIGKEITEN PRÜFEN
# ============================================================================
def check_dependencies() -> list[str]:
    """
    Prüft alle Pflichtmodule und gibt eine Liste fehlender Pakete zurück.
    """
    missing: list[str] = []

    try:
        import tkinter  # noqa: F401
    except ImportError:
        missing.append(
            "tkinter  ->  Linux: sudo apt install python3-tk\n"
            "             Windows: Python neu installieren, 'tcl/tk' anhaken"
        )

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        missing.append("customtkinter  ->  pip install customtkinter")

    try:
        import psutil  # noqa: F401
    except ImportError:
        missing.append("psutil  ->  pip install psutil")

    # sounddevice ist optional - fehlt es, funktioniert die App weiterhin
    # (Aufnahme läuft komplett über FFmpeg), aber die komplette Live-
    # Pegelvorschau im Audio-Tab (Mikrofon UND Lautsprecher) bleibt dann
    # auf "nicht verfügbar", nicht nur die Geräte-Erkennung dort.
    return missing


def _set_windows_dpi_awareness():
    """
    Aktiviert HiDPI-Unterstützung unter Windows - PER-MONITOR statt nur
    system-weit, damit die Schrift auch nach dem Verschieben des Fensters
    auf einen Bildschirm mit ANDEREM Skalierungsfaktor scharf bleibt
    (bei reiner System-DPI-Awareness würde Windows das Fenster in diesem
    Fall einfach hochskalieren -> unscharfe/verwaschene Schrift).

    Versucht die modernste verfügbare API zuerst und fällt bei älteren
    Windows-Versionen stufenweise zurück:
      1. SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2) - Windows 10 1703+
      2. SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) - Windows 8.1+
      3. SetProcessDPIAware() - Windows Vista+ (nur System-DPI, letzter Ausweg)
    Schlagen alle fehl (z. B. weil gar nicht unter Windows ausgeführt),
    wird das schlicht ignoriert - rein kosmetisch, kein Startabbruch wert.
    """
    try:
        import ctypes
    except Exception:
        return

    # 1) Per-Monitor V2 (modernste Variante)
    try:
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return
    except Exception:
        pass

    # 2) Per-Monitor (aeltere, aber immer noch per-Monitor-fähige API)
    try:
        PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        return
    except Exception:
        pass

    # 3) Letzter Ausweg: nur System-DPI (besser als gar nichts)
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ============================================================================
# 2) PLATTFORM-VORBEREITUNG
# ============================================================================
def prepare_platform() -> bool:
    """
    Führt plattformspezifische Initialisierung durch.
    :return: True, wenn die App starten darf
    """
    # ---- Windows: scharfe Schrift auf HiDPI-Displays ----
    if sys.platform.startswith("win"):
        _set_windows_dpi_awareness()
        return True

    # ---- Linux: ohne DISPLAY kein GUI-Start ----
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            print(
                "FEHLER: Kein Display gefunden.\n"
                "ScreenRec Pro benötigt eine grafische Sitzung "
                "(nicht über reines SSH/TTY startbar).",
                file=sys.stderr,
            )
            return False

    return True


# ============================================================================
# 3) HAUPTFUNKTION
# ============================================================================
def main() -> int:
    # --- Abhängigkeiten ---
    missing = check_dependencies()
    if missing:
        print("=" * 62, file=sys.stderr)
        print(" FEHLENDE ABHÄNGIGKEITEN", file=sys.stderr)
        print("=" * 62, file=sys.stderr)
        for item in missing:
            print(f"  • {item}", file=sys.stderr)
        print("=" * 62, file=sys.stderr)
        return 1

    # --- Plattform ---
    if not prepare_platform():
        return 1

    # --- Import erst NACH der Prüfung (sonst crasht der Import) ---
    from gui_main import MainWindow

    try:
        app = MainWindow()
        app.mainloop()
        return 0
    except KeyboardInterrupt:
        print("\nDurch Benutzer abgebrochen.")
        return 0
    except Exception as exc:
        print(f"Unerwarteter Fehler: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())