"""
platform_utils.py
=================
Kapselt ALLE betriebssystemspezifischen Unterschiede an einer einzigen Stelle.

Damit bleibt der restliche Code plattformneutral und der Sprung
Linux-Test -> Windows-EXE erfordert keine Änderungen an der Logik.
"""

import os
import shutil
import subprocess
import sys

# ----------------------------------------------------------------------------
# PLATTFORM-FLAGS (einmalig berechnet, überall importierbar)
# ----------------------------------------------------------------------------
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"


# ============================================================================
# 1) DISPLAY-SERVER-ERKENNUNG (nur Linux relevant)
# ============================================================================
def get_session_type() -> str:
    """
    Ermittelt den aktiven Display-Server unter Linux.

    :return: 'x11', 'wayland' oder 'unknown'
    """
    if not IS_LINUX:
        return "native"

    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session in ("x11", "wayland"):
        return session

    # Fallback-Heuristik über gesetzte Umgebungsvariablen
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def get_x11_display() -> str:
    """
    Liefert die DISPLAY-Variable für x11grab (Standard ':0.0').
    """
    display = os.environ.get("DISPLAY", ":0")
    # x11grab erwartet das Format ':0.0' - Screen-Nummer ergänzen falls nötig
    if "." not in display.split(":")[-1]:
        display = f"{display}.0"
    return display


def get_platform_warning() -> str | None:
    """
    Gibt einen Warnhinweis zurück, falls die Umgebung problematisch ist.
    Wird in der GUI-Statusleiste angezeigt.
    """
    if IS_LINUX and get_session_type() == "wayland":
        return ("Wayland erkannt - Bildschirmaufnahme via x11grab ist "
                "eingeschränkt. Bitte in einer X11/Xorg-Sitzung anmelden.")
    return None


def has_x11_compositor() -> bool:
    """
    Prüft, ob unter X11 ein Compositor aktiv ist (picom, mutter, kwin ...).

    Wichtig für die Bereichsauswahl (region_selector.py): Fenstertransparenz
    ('-alpha') wird von Tk/X11 nur dann tatsächlich dargestellt, wenn ein
    Compositor läuft UND das Fenster von ihm 'redirected' wird. Ohne
    Compositor - was auf genau den schwachen/alten Notebooks, für die diese
    App optimiert ist, mit schlanken Fenstermanagern (i3, Openbox, LXDE ...)
    häufig der Fall ist - bleibt das Overlay komplett blickdicht
    (schwarzer Bildschirm), obwohl der Code korrekt ist.

    Windows/macOS kompositieren immer systemweit (DWM/Quartz), daher dort
    stets True.

    :return: True, wenn ein Compositor sicher aktiv ist. Im Zweifel
             (z. B. 'xprop' fehlt) wird konservativ False geliefert, damit
             die Bereichsauswahl auf den robusteren Screenshot-Modus
             zurückfällt statt auf einem schwarzen Bildschirm zu enden.
    """
    if not IS_LINUX:
        return True

    try:
        result = subprocess.run(
            ["xprop", "-root", "_NET_WM_CM_S0"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return False
        # Vorhanden:      "_NET_WM_CM_S0(WINDOW): window id # 0x..."
        # Nicht vorhanden: "_NET_WM_CM_S0:  no such atom on any window."
        return "no such atom" not in result.stdout.lower()
    except Exception:
        return False


# ============================================================================
# 2) BILDSCHIRMAUFLÖSUNG ERMITTELN
# ============================================================================
def get_screen_size(fallback=(1920, 1080)) -> tuple[int, int]:
    """
    Ermittelt die primäre Bildschirmauflösung.

    Nutzt Tkinter (immer vorhanden, da GUI-Framework),
    fällt bei Fehlern auf einen Standardwert zurück.
    """
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        width = root.winfo_screenwidth()
        height = root.winfo_screenheight()
        root.destroy()
        # Gerade Zahlen erzwingen (Pflicht für yuv420p)
        return (width - width % 2, height - height % 2)
    except Exception:
        return fallback


# ============================================================================
# 3) SUBPROCESS-FLAGS
# ============================================================================
def get_subprocess_flags() -> dict:
    """
    Liefert Keyword-Argumente für subprocess.Popen.

    Windows: unterdrückt das Aufblitzen eines Konsolenfensters.
    Linux/macOS: leeres Dict (nicht nötig).
    """
    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NO_WINDOW,
        }
    return {}


# ============================================================================
# 4) DATEI-/ORDNER-AKTIONEN
# ============================================================================
def open_file_manager(path: str) -> None:
    """
    Öffnet den Zielordner im nativen Dateimanager
    (Explorer / Nautilus / Finder).
    """
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if IS_WINDOWS:
            os.startfile(folder)  # type: ignore[attr-defined]
        elif IS_MACOS:
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception:
        pass


def get_default_videos_dir() -> str:
    """
    Ermittelt den Standard-Videoordner des Nutzers.

    Linux: respektiert XDG-User-Dirs (z. B. ~/Videos oder ~/Videos lokalisiert)
    Windows: ~/Videos
    """
    home = os.path.expanduser("~")

    if IS_LINUX:
        # XDG-User-Dirs auslesen (kann lokalisiert sein, z. B. "Videos")
        try:
            result = subprocess.run(
                ["xdg-user-dir", "VIDEOS"],
                capture_output=True, text=True, timeout=3,
            )
            candidate = result.stdout.strip()
            if candidate and os.path.isdir(candidate):
                return candidate
        except Exception:
            pass

    for name in ("Videos", "Movies", "Filme"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate

    return home