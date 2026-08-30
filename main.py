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

    # sounddevice ist optional (nur Fallback für Geräteerkennung)
    return missing


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
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
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