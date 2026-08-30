"""
ffmpeg_utils.py
===============
FFmpeg-Pfadauflösung und Kommando-Builder - vollständig cross-platform.

Pfadstrategie:
  * Windows: bevorzugt die eingebettete/lokale bin/ffmpeg.exe
             (PyInstaller entpackt sie nach sys._MEIPASS)
  * Linux:   bevorzugt das systemweite ffmpeg aus $PATH

Capture-Backends:
  * Windows -> gdigrab (Video) + dshow (Audio)
  * Linux   -> x11grab (Video) + pulse (Audio)
"""

import os
import shutil
import sys

from config import (
    AUDIO_BITRATE,
    AUDIO_CODEC,
    AUDIO_SAMPLERATE,
    CRF_X264,
    CRF_X265,
    PIXEL_FORMAT,
)
from platform_utils import (
    IS_LINUX,
    IS_WINDOWS,
    get_screen_size,
    get_x11_display,
)


# ============================================================================
# 1) RESSOURCEN- UND FFMPEG-PFADAUFLÖSUNG
# ============================================================================
def resource_path(relative_path: str) -> str:
    """
    Löst einen Pfad relativ zum Anwendungsverzeichnis auf.

    - Normalbetrieb (python main.py): Verzeichnis dieser Datei
    - PyInstaller-EXE: sys._MEIPASS (temporärer Entpackordner)

    Genau diese getattr-Prüfung sorgt dafür, dass die eingebettete
    ffmpeg.exe später innerhalb der .exe gefunden wird.
    """
    base_path = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base_path, relative_path)


def get_ffmpeg_path() -> str:
    """
    Ermittelt den Pfad zur FFmpeg-Binary.

    Suchreihenfolge:
      Windows: bin/ffmpeg.exe -> ./ffmpeg.exe -> neben der EXE -> $PATH
      Linux:   $PATH -> bin/ffmpeg -> ./ffmpeg

    :raises FileNotFoundError: wenn nichts gefunden wurde
    """
    exe_name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"

    # --- Unter Linux hat das Systempaket Vorrang -------------------------
    if not IS_WINDOWS:
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg

    # --- Gebündelte / lokale Binary --------------------------------------
    candidates = [
        resource_path(os.path.join("bin", exe_name)),
        resource_path(exe_name),
    ]

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, exe_name))
        candidates.append(os.path.join(exe_dir, "bin", exe_name))

    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        if os.path.isfile(candidate) and IS_WINDOWS:
            return candidate

    # --- Letzter Versuch: systemweites FFmpeg ----------------------------
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    if IS_WINDOWS:
        raise FileNotFoundError(
            "ffmpeg.exe wurde nicht gefunden.\n\n"
            "Bitte lege 'ffmpeg.exe' im Unterordner 'bin/' ab\n"
            "oder installiere FFmpeg systemweit (PATH-Variable)."
        )
    raise FileNotFoundError(
        "FFmpeg wurde nicht gefunden.\n\n"
        "Installiere es mit:\n"
        "  sudo apt install ffmpeg        (Debian/Ubuntu)\n"
        "  sudo dnf install ffmpeg        (Fedora)\n"
        "  sudo pacman -S ffmpeg          (Arch)"
    )


def check_encoder_available(encoder: str) -> bool:
    """
    Prüft, ob der gewünschte Encoder in der FFmpeg-Build enthalten ist.
    Nützlich unter Linux, wo manche Distros libx265 separat paketieren.
    """
    import subprocess
    from platform_utils import get_subprocess_flags

    try:
        result = subprocess.run(
            [get_ffmpeg_path(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
            **get_subprocess_flags(),
        )
        return encoder in result.stdout
    except Exception:
        return True  # Im Zweifel erlauben


# ============================================================================
# 2) VIDEO-EINGABE (plattformabhängig)
# ============================================================================
def build_video_input_args(mode_region: bool, region: tuple | None, fps: str) -> list:
    """
    Baut die Video-Eingabeparameter für den jeweiligen Screen-Grabber.

    Windows -> gdigrab mit -offset_x / -offset_y
    Linux   -> x11grab mit ':0.0+X,Y'
    """
    args: list[str] = []

    # ---------------- WINDOWS: gdigrab -----------------------------------
    if IS_WINDOWS:
        args += [
            "-f", "gdigrab",
            "-framerate", str(fps),
            "-draw_mouse", "1",
            "-thread_queue_size", "512",
        ]
        if mode_region and region:
            x, y, w, h = _sanitize_region(region)
            args += [
                "-offset_x", str(x),
                "-offset_y", str(y),
                "-video_size", f"{w}x{h}",
                "-i", "desktop",
            ]
        else:
            args += ["-i", "desktop"]
        return args

    # ---------------- LINUX: x11grab -------------------------------------
    display = get_x11_display()
    args += [
        "-f", "x11grab",
        "-framerate", str(fps),
        "-draw_mouse", "1",
        "-thread_queue_size", "512",
        # Vermeidet den 'Xlib: extension XFIXES missing'-Overhead
        "-probesize", "32M",
    ]

    if mode_region and region:
        x, y, w, h = _sanitize_region(region)
        args += [
            "-video_size", f"{w}x{h}",
            "-i", f"{display}+{x},{y}",
        ]
    else:
        # x11grab benötigt IMMER eine explizite Größe
        sw, sh = get_screen_size()
        args += [
            "-video_size", f"{sw}x{sh}",
            "-i", display,
        ]
    return args


def _sanitize_region(region: tuple) -> tuple[int, int, int, int]:
    """
    Erzwingt gerade Breiten-/Höhenwerte (Pflicht für yuv420p-Subsampling).
    """
    x, y, w, h = (int(v) for v in region)
    w -= w % 2
    h -= h % 2
    return max(0, x), max(0, y), max(2, w), max(2, h)


# ============================================================================
# 3) AUDIO-EINGABE (plattformabhängig)
# ============================================================================
def build_audio_input_args(audio_device: str | None) -> list:
    """
    Baut die Audio-Eingabeparameter.

    Windows -> dshow  (-i audio="Mikrofon (Realtek)")
    Linux   -> pulse  (-i alsa_input.pci-0000_00_1f.3.analog-stereo)
    """
    if not audio_device:
        return []

    if IS_WINDOWS:
        return [
            "-f", "dshow",
            "-thread_queue_size", "1024",
            "-audio_buffer_size", "80",
            "-i", f"audio={audio_device}",
        ]

    # Linux: PulseAudio / PipeWire (pipewire-pulse ist API-kompatibel)
    return [
        "-f", "pulse",
        "-thread_queue_size", "1024",
        "-fragment_size", "1024",
        "-i", audio_device,
    ]


# ============================================================================
# 4) OUTPUT-ENCODING
# ============================================================================
def build_output_args(encoder: str, preset: str, has_audio: bool) -> list:
    """
    Baut die Encoding-Parameter - identisch auf allen Plattformen.
    """
    crf = CRF_X265 if encoder == "libx265" else CRF_X264

    args = [
        "-c:v", encoder,
        "-preset", preset,
        "-crf", crf,
        # Nulllatenz-Tuning: reduziert RAM-Bedarf & Lookahead-Rechenlast
        "-tune", "zerolatency",
        "-pix_fmt", PIXEL_FORMAT,
        "-g", "60",                    # Keyframe alle 2 s bei 30 FPS
        "-movflags", "+faststart",     # MP4 sofort abspielbar
    ]

    if encoder == "libx265":
        args += ["-tag:v", "hvc1"]

    if has_audio:
        args += [
            "-c:a", AUDIO_CODEC,
            "-b:a", AUDIO_BITRATE,
            "-ar", AUDIO_SAMPLERATE,
            "-ac", "2",
        ]

    return args


# ============================================================================
# 5) KOMPLETTE KOMMANDOS
# ============================================================================
def build_record_command(
    output_path: str,
    fps: str,
    encoder: str,
    preset: str,
    mode_region: bool = False,
    region: tuple | None = None,
    audio_device: str | None = None,
) -> list:
    """
    Setzt das vollständige FFmpeg-Aufnahmekommando zusammen.
    """
    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    cmd += build_video_input_args(mode_region, region, fps)
    cmd += build_audio_input_args(audio_device)

    has_audio = bool(audio_device)
    cmd += build_output_args(encoder, preset, has_audio)

    if has_audio:
        cmd += ["-shortest"]

    cmd.append(output_path)
    return cmd


def build_screenshot_command(output_path: str, width: int, height: int) -> list:
    """
    Baut ein FFmpeg-Kommando für EINEN einzelnen Vollbild-Screenshot
    (PNG). Wird ausschließlich als Fallback für die Bereichsauswahl
    genutzt, wenn unter Linux/X11 kein Compositor läuft und die
    Fenstertransparenz des Overlays deshalb nicht funktioniert
    (siehe platform_utils.has_x11_compositor).

    Ein einmaliger Screenshot beim Öffnen der Bereichsauswahl ist
    ressourcentechnisch vernachlässigbar - im Gegensatz zu einer
    laufenden Aufnahme wird hier nur ein einziges Bild gezogen.
    """
    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    if IS_WINDOWS:
        cmd += ["-f", "gdigrab", "-i", "desktop"]
    else:
        display = get_x11_display()
        cmd += ["-f", "x11grab", "-video_size", f"{width}x{height}", "-i", display]

    cmd += ["-frames:v", "1", output_path]
    return cmd


def build_benchmark_command(output_path: str, duration: int, fps: str = "30") -> list:
    """
    Kurze Testaufnahme für den Benchmark.
    Bewusst mit preset 'medium', um die CPU realistisch zu belasten.
    """
    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    cmd += build_video_input_args(mode_region=False, region=None, fps=fps)

    cmd += [
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", CRF_X264,
        "-pix_fmt", PIXEL_FORMAT,
        output_path,
    ]
    return cmd