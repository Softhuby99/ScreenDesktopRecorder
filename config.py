"""
config.py
=========
Zentrale Konfigurationskonstanten für ScreenRec Pro.
Plattformunabhängig - enthält nur Werte, keine Logik.
"""

# ----------------------------------------------------------------------------
# ANWENDUNGS-METADATEN
# ----------------------------------------------------------------------------
APP_NAME = "ScreenRec Pro"
APP_VERSION = "1.1.0"

# ----------------------------------------------------------------------------
# FARBPALETTE (Windows 11 Fluent / Dark Mode - Anthrazit)
# ----------------------------------------------------------------------------
COLOR_BG_MAIN = "#1B1B1F"      # Fensterhintergrund (Anthrazit)
COLOR_BG_CARD = "#25252B"      # Karten-/Panel-Hintergrund
COLOR_BG_INPUT = "#2F2F37"     # Eingabefelder / Dropdowns
COLOR_BG_HOVER = "#3A3A44"     # Hover-Zustand neutraler Elemente

COLOR_ACCENT = "#0E7AD4"       # Akzentblau
COLOR_ACCENT_HOVER = "#1A8CE8"
COLOR_DANGER = "#D13438"       # Stopp-Button Rot
COLOR_DANGER_HOVER = "#E04A4E"
COLOR_SUCCESS = "#3FA34D"
COLOR_WARNING = "#E8A33D"

COLOR_TEXT_PRIMARY = "#F2F2F5"
COLOR_TEXT_MUTED = "#9A9AA5"

# Einheitliche Rundungen für das Fluent-Design
RADIUS_LG = 12
RADIUS_MD = 8
RADIUS_SM = 6

# ----------------------------------------------------------------------------
# ENCODING-PRESETS
# ----------------------------------------------------------------------------
FPS_OPTIONS = ["24", "30", "60"]
DEFAULT_FPS = "30"

ENCODER_OPTIONS = ["libx264", "libx265"]
DEFAULT_ENCODER = "libx264"

PRESET_OPTIONS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]
DEFAULT_PRESET = "faster"

CRF_X264 = "23"
CRF_X265 = "25"

AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"
AUDIO_SAMPLERATE = "48000"
NO_AUDIO_LABEL = "Kein Audio (nur Bild)"

PIXEL_FORMAT = "yuv420p"

# ----------------------------------------------------------------------------
# BENCHMARK-ENTSCHEIDUNGSMATRIX
# ----------------------------------------------------------------------------
BENCHMARK_DURATION = 5          # Sekunden Testaufnahme
BENCHMARK_SAMPLE_INTERVAL = 1.0 # psutil-Messintervall

BENCHMARK_TIERS = [
    {
        "max_cpu": 60.0,
        "fps": "60",
        "encoder": "libx264",
        "preset": "medium",
        "title": "Hervorragende Performance!",
        "message": "Hervorragende Performance! 60 FPS aktiviert.",
        "color": COLOR_SUCCESS,
    },
    {
        "max_cpu": 85.0,
        "fps": "30",
        "encoder": "libx264",
        "preset": "faster",
        "title": "Gute Performance",
        "message": "Gute Performance. 30 FPS für flüssige Aufnahmen gewählt.",
        "color": COLOR_ACCENT,
    },
    {
        "max_cpu": 1000.0,  # Auffang-Tier (alles über 85 %)
        "fps": "30",
        "encoder": "libx264",
        "preset": "ultrafast",
        "title": "Eco-Modus aktiviert",
        "message": ("Eco-Modus aktiviert. Optimiert für ältere Hardware, "
                    "um Ruckeln zu verhindern."),
        "color": COLOR_WARNING,
    },
]

# ----------------------------------------------------------------------------
# AUFNAHME-MODI
# ----------------------------------------------------------------------------
MODE_FULLSCREEN = "Vollbild"
MODE_REGION = "Bereich wählen"
MODE_OPTIONS = [MODE_FULLSCREEN, MODE_REGION]