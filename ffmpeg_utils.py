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
    QSV_ENCODERS,
    QSV_GLOBAL_QUALITY,
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


def check_qsv_available(encoder: str, timeout: int = 8) -> bool:
    """
    Prüft, ob ein Intel-Quick-Sync-Encoder (h264_qsv/hevc_qsv/av1_qsv)
    tatsächlich benutzt werden kann.

    Reines Vorhandensein im FFmpeg-Build (check_encoder_available) reicht
    bei QSV NICHT aus - das sagt nur, dass FFmpeg mit QSV-Unterstützung
    kompiliert wurde, nicht ob die lokale Intel-GPU/Treiber/Kernel-Version
    die Kodierung tatsächlich beherrschen (v. a. av1_qsv braucht eine
    neuere Intel-iGPU-Generation). Deshalb wird zusätzlich eine winzige
    Testkodierung durchgeführt.
    """
    import subprocess
    from platform_utils import get_subprocess_flags

    if not check_encoder_available(encoder):
        return False

    try:
        cmd = [
            get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
            "-frames:v", "5",
            "-c:v", encoder,
            "-global_quality", QSV_GLOBAL_QUALITY,
            "-f", "null", "-",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            **get_subprocess_flags(),
        )
        return result.returncode == 0
    except Exception:
        return False


# ============================================================================
# 2) VIDEO-EINGABE (plattformabhängig)
# ============================================================================
def build_video_input_args(
    mode_region: bool, region: tuple | None, fps: str,
    screen_size: tuple[int, int] | None = None,
) -> list:
    """
    Baut die Video-Eingabeparameter für den jeweiligen Screen-Grabber.

    Windows -> gdigrab mit -offset_x / -offset_y
    Linux   -> x11grab mit ':0.0+X,Y'

    screen_size: im Vollbild-Modus unter Linux benötigt x11grab eine
    explizite Größe. Wird screen_size vom Aufrufer mitgegeben (empfohlen -
    siehe RecorderThread/BenchmarkThread, die dies bereits vom GUI-Thread
    ermittelt bekommen), wird DAMIT gearbeitet, statt selbst
    get_screen_size() aufzurufen - diese Funktion läuft in einem
    Worker-Thread, und get_screen_size() öffnet dafür ein eigenes
    Tk-Root, was aus einem Nicht-GUI-Thread nicht sicher ist. Nur wenn
    kein screen_size übergeben wurde (z. B. Aufruf aus einem Kontext ohne
    laufende GUI), wird get_screen_size() als Fallback genutzt.
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
        sw, sh = screen_size if screen_size else get_screen_size()
        args += [
            "-video_size", f"{sw}x{sh}",
            "-i", display,
        ]
    return args


def _sanitize_region(region: tuple) -> tuple[int, int, int, int]:
    """
    Erzwingt gerade Breiten-/Höhenwerte (Pflicht für yuv420p-Subsampling).

    x/y werden BEWUSST NICHT auf 0 nach unten begrenzt: gdigrabs
    -offset_x/-offset_y sind relativ zum virtuellen Desktop-Ursprung, der
    bei einem links von/oberhalb des Hauptbildschirms platzierten Monitor
    (gängiges Windows-Multi-Monitor-Layout) legitim NEGATIV ist. Ein
    max(0, x) würde die Aufnahme dann auf den Hauptbildschirm zurückwerfen,
    statt den vom Nutzer ausgewählten (negativ liegenden) Bereich
    aufzunehmen - siehe region_selector.py, das den echten (ggf.
    negativen) Ursprung über winfo_vrootx()/winfo_vrooty() ermittelt.
    """
    x, y, w, h = (int(v) for v in region)
    w -= w % 2
    h -= h % 2
    return x, y, max(2, w), max(2, h)


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
            # 80 ms war zu knapp bemessen: sobald Bildschirmaufnahme +
            # Encoding kurzzeitig mehr CPU-Zeit brauchen (normal, gerade
            # bei langsameren Encoder-Presets), lief der dshow-Puffer
            # dabei ueber - hoerbar als starke Verzerrung/Knacken in der
            # Audiospur, und im schlimmsten Fall bricht der Audio-Stream
            # dabei komplett ab (siehe -shortest weiter unten, das dann
            # die GESAMTE Aufnahme auf wenige KB zusammenstutzt). 500 ms
            # ist FFmpegs eigener Standardwert fuer dshow und deutlich
            # toleranter gegenueber kurzen CPU-Spitzen - die dadurch
            # etwas hoehere Aufnahmelatenz spielt bei einem Rekorder
            # (anders als bei Livestreaming) keine Rolle.
            "-audio_buffer_size", "500",
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
def _build_audio_filter_args(gain: float, denoise: bool) -> list:
    """
    Baut eine optionale '-af'-Filterkette für die Mikrofon-Aufnahme:
      - afftdn:    einfache Rauschunterdrückung (Wunsch: "Performance vom Mikro")
      - volume:    digitale Verstärkung/Abschwächung (Wunsch: "Lautstärke vom Mikro")
      - alimiter:  Sicherheitsnetz GEGEN digitales Clipping (siehe unten)
    Wird nur angehängt, wenn tatsächlich etwas zu verändern ist - eine
    leere Filterkette würde FFmpeg unnötig zusätzliche Arbeit aufbürden.
    """
    filters = []
    if denoise:
        filters.append("afftdn")
    boosted = gain is not None and gain > 1.0 + 0.005
    if gain is not None and abs(gain - 1.0) > 0.005:
        filters.append(f"volume={gain:.3f}")
    if boosted:
        # Ohne Begrenzer fuehrt "volume" bei Werten > 1.0 (Regler geht bis
        # 3x = +9.5 dB) auf einem bereits normal ausgesteuerten Mikrofon
        # fast zwangslaeufig zu hartem digitalem Clipping - genau das vom
        # Nutzer gemeldete "klingt nur noch super verzerrt". alimiter
        # deckelt Spitzen sanft VOR der Vollaussteuerung, statt sie
        # abzuschneiden, und wird nur aktiv, wenn ueberhaupt verstaerkt wird.
        filters.append("alimiter=limit=0.95")
    return ["-af", ",".join(filters)] if filters else []


def _gop_size(fps: str) -> str:
    """
    Keyframe-Abstand: IMMER 2 Sekunden, unabhängig von der gewählten
    Framerate. Ein fest verdrahtetes "-g 60" wäre nur bei 30 FPS wirklich
    2s (bei 60 FPS wären es 1s, bei 24 FPS ~2.5s) - hier stattdessen an
    die tatsächliche fps gekoppelt.
    """
    try:
        value = int(round(float(fps) * 2))
    except (TypeError, ValueError):
        value = 60
    return str(max(2, value))


def build_output_args(
    encoder: str, preset: str, has_audio: bool, audio_only: bool = False,
    gain: float = 1.0, denoise: bool = False, fps: str = "30",
) -> list:
    """
    Baut die Encoding-Parameter - identisch auf allen Plattformen.

    Bei audio_only=True wird JEDER Video-Parameter ausgelassen (kein
    Encoder, kein CRF, kein Preset) - es gibt schlicht keinen
    Video-Stream, der kodiert werden müsste.

    gain/denoise wirken NUR auf die Audiospur (Mikrofon-"Verstärkung" und
    einfache Rauschunterdrückung) und werden komplett ignoriert, wenn
    has_audio=False ist.
    """
    audio_filter_args = _build_audio_filter_args(gain, denoise) if has_audio else []
    gop = _gop_size(fps)

    if audio_only:
        return [
            "-vn",  # explizit keine Video-Ausgabe
            "-c:a", AUDIO_CODEC,
            "-b:a", AUDIO_BITRATE,
            "-ar", AUDIO_SAMPLERATE,
            "-ac", "2",
            *audio_filter_args,
        ]

    is_qsv = encoder in QSV_ENCODERS

    if is_qsv:
        # Intel Quick Sync kennt weder '-crf' noch '-tune zerolatency' und
        # unterstützt die Presets 'ultrafast'/'superfast' von libx264/265
        # nicht - auf 'veryfast' abbilden statt einen FFmpeg-Fehler zu riskieren.
        qsv_preset = "veryfast" if preset in ("ultrafast", "superfast") else preset
        args = [
            "-c:v", encoder,
            "-preset", qsv_preset,
            "-global_quality", QSV_GLOBAL_QUALITY,
            "-pix_fmt", PIXEL_FORMAT,
            "-g", gop,
            "-movflags", "+faststart",
        ]
        if encoder == "hevc_qsv":
            args += ["-tag:v", "hvc1"]
    else:
        crf = CRF_X265 if encoder == "libx265" else CRF_X264
        args = [
            "-c:v", encoder,
            "-preset", preset,
            "-crf", crf,
            # Nulllatenz-Tuning: reduziert RAM-Bedarf & Lookahead-Rechenlast
            "-tune", "zerolatency",
            "-pix_fmt", PIXEL_FORMAT,
            "-g", gop,                     # Keyframe alle 2 s (an fps gekoppelt)
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
            *audio_filter_args,
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
    audio_only: bool = False,
    gain: float = 1.0,
    denoise: bool = False,
    screen_size: tuple[int, int] | None = None,
) -> list:
    """
    Setzt das vollständige FFmpeg-Aufnahmekommando zusammen.

    audio_only=True überspringt die komplette Video-Eingabe (kein
    gdigrab/x11grab) - es wird ausschließlich die Audioquelle aufgezeichnet.
    Ein audio_device ist in diesem Fall zwingend erforderlich (wird von
    der GUI vor dem Start erzwungen).

    gain/denoise betreffen ausschließlich die Mikrofonspur (Verstärkung /
    einfache Rauschunterdrückung) und werden ignoriert, wenn kein
    audio_device gesetzt ist.
    """
    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    if not audio_only:
        cmd += build_video_input_args(mode_region, region, fps, screen_size=screen_size)
    cmd += build_audio_input_args(audio_device)

    has_audio = bool(audio_device) or audio_only
    cmd += build_output_args(
        encoder, preset, has_audio, audio_only=audio_only, gain=gain, denoise=denoise,
        fps=fps,
    )

    # ABSICHTLICH KEIN "-shortest" mehr: Video- und Audio-Input laufen beide
    # durchgehend und werden gemeinsam per 'q' beendet, sollten also ohnehin
    # fast exakt gleich lang sein. "-shortest" beendet die GESAMTE Ausgabe
    # aber sofort, sobald IRGENDEIN Stream endet - bricht z. B. unter
    # Windows kurzzeitig der dshow-Audio-Stream ab (Puffer-Überlauf,
    # Gerät kurz belegt o. ä.), stutzt das die komplette, ansonsten
    # einwandfrei laufende Videoaufnahme auf wenige Sekunden/KB zusammen,
    # OHNE dass FFmpeg dabei einen Fehler meldet (sauberer Exit-Code) -
    # genau das vom Nutzer gemeldete Symptom "Aufnahme nur ein paar KB
    # groß". Ohne "-shortest" bleibt die Videospur in so einem Fall
    # vollständig erhalten, die Audiospur endet dann eben etwas früher.

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


def build_benchmark_command(
    output_path: str, duration: int, fps: str = "30",
    screen_size: tuple[int, int] | None = None,
) -> list:
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

    cmd += build_video_input_args(mode_region=False, region=None, fps=fps, screen_size=screen_size)

    cmd += [
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", CRF_X264,
        "-pix_fmt", PIXEL_FORMAT,
        output_path,
    ]
    return cmd