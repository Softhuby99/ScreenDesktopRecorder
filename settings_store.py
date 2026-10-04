"""
settings_store.py
=================
Merkt sich die zuletzt benutzten Einstellungen zwischen zwei
Programmstarts (Tonquelle, Speicherort, Bildrate, Encoder, Preset,
Mikrofon-Verstärkung, Rauschunterdrückung).

Hintergrund: Früher stand die Tonquelle nach JEDEM Start wieder auf
"Kein Audio (nur Bild)". Wer nicht jedes Mal daran gedacht hat, sie neu
auszuwählen, bekam Videos ohne Ton.

Speicherort der Datei:
  Windows: %APPDATA%\\ScreenRecPro\\settings.json
  Linux:   $XDG_CONFIG_HOME/ScreenRecPro/settings.json
           (Standard: ~/.config/ScreenRecPro/settings.json)

Bewusst NICHT neben der .exe: eine mit PyInstaller --onefile gebaute
.exe entpackt sich bei jedem Start in einen neuen Temp-Ordner, und der
Ordner der .exe selbst ist oft schreibgeschützt (z. B. Programme).

Alle Fehler werden geschluckt - fehlende oder kaputte Einstellungen
dürfen den Programmstart nie verhindern, es gelten dann einfach die
Standardwerte.
"""

import json
import os

from platform_utils import IS_WINDOWS

_APP_DIR_NAME = "ScreenRecPro"
_FILE_NAME = "settings.json"


def settings_path() -> str:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    return os.path.join(base, _APP_DIR_NAME, _FILE_NAME)


def load_settings() -> dict:
    """:return: gespeicherte Einstellungen oder {} (nie eine Exception)."""
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> bool:
    """
    Schreibt die Einstellungen atomar (erst in eine Temp-Datei, dann
    umbenennen) - ein Absturz mitten im Schreiben hinterlässt so nie eine
    halbe, unlesbare Datei.

    :return: True bei Erfolg
    """
    path = settings_path()
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False
