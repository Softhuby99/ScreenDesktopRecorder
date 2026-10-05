"""
ffmpeg_utils.py
===============
FFmpeg-Pfadauflösung und Kommando-Builder - vollständig cross-platform.

Pfadstrategie:
  * Windows: bevorzugt die eingebettete/lokale bin/ffmpeg.exe
             (PyInstaller entpackt sie nach sys._MEIPASS)
  * Linux:   bevorzugt das systemweite ffmpeg aus $PATH

Capture-Backends:
  * Windows -> ddagrab/gdigrab (Video) + dshow (Audio)
  * Linux   -> x11grab (Video) + pulse (Audio)

Ton/Bild-Synchronisation unter Windows: siehe build_record_command und
_build_audio_sync_filters - beide Quellen bekommen dieselbe Uhr.
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
_DDAGRAB_CACHE: dict[str, bool] = {}


def check_ddagrab_available(timeout: int = 10) -> bool:
    """
    Prüft, ob die Desktop Duplication API (ddagrab) nutzbar ist.

    Hintergrund: gdigrab nutzt die alte GDI-Schnittstelle (BitBlt) und
    ist auf modernen Windows-Systemen mit zusammengesetztem Desktop
    dramatisch langsam - eine Messung auf echter Hardware ergab bei
    1920x1080 nur 3-22 statt der angeforderten 30 Bilder/s, was zu
    winzigen, ruckeligen Aufnahmen führt. ddagrab (Windows 8+, D3D11,
    FFmpeg >= 6) holt die Bilder direkt von der GPU und erreichte auf
    derselben Hardware 29,9 fps.

    Ergebnis wird zwischengespeichert - der Test kostet ~1 s und das
    Ergebnis ändert sich zur Laufzeit nicht.
    """
    if not IS_WINDOWS:
        return False
    if "ok" in _DDAGRAB_CACHE:
        return _DDAGRAB_CACHE["ok"]

    import subprocess
    from platform_utils import get_subprocess_flags

    ok = False
    try:
        cmd = [
            get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=5",
            "-frames:v", "3",
            "-vf", "hwdownload,format=bgra",
            "-f", "null", "-",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            **get_subprocess_flags(),
        )
        ok = result.returncode == 0
    except Exception:
        ok = False

    _DDAGRAB_CACHE["ok"] = ok
    return ok


def _should_use_ddagrab(
    mode_region: bool, region: tuple | None,
    screen_size: tuple[int, int] | None = None,
) -> bool:
    """
    Entscheidet, ob ddagrab statt gdigrab benutzt werden kann.

    Ausschlussgründe - in allen Fällen wird auf gdigrab zurückgefallen,
    das den gesamten virtuellen Desktop abdeckt:
      * Die Desktop Duplication API steht nicht zur Verfügung (zu altes
        Windows, kein D3D11, FFmpeg ohne ddagrab).
      * Der gewählte Bereich liegt links/oberhalb des Hauptmonitors
        (negative Koordinaten im virtuellen Desktop). ddagrab rechnet
        relativ zum jeweiligen Monitor und kann das nicht abbilden;
        gdigrab dagegen schon (siehe _sanitize_region).
      * Der Bereich ragt über den Hauptmonitor hinaus, liegt also
        (teilweise) auf einem zweiten Monitor rechts/unterhalb. ddagrab
        nimmt hier nur output_idx=0 auf und würde entweder abbrechen
        oder den falschen Ausschnitt liefern. Diese Prüfung ist wichtig,
        weil die einmalige Verfügbarkeitsprüfung nur den Vollbildfall
        testet - ein erst zur Laufzeit scheiternder Bereichsaufruf hätte
        keine Rückfallebene mehr.
    """
    if not IS_WINDOWS:
        return False

    if mode_region and region:
        x, y, w, h = _sanitize_region(region)
        if x < 0 or y < 0:
            return False
        if screen_size:
            sw, sh = screen_size
            if x + w > sw or y + h > sh:
                return False

    return check_ddagrab_available()


def build_video_filter_args(use_ddagrab: bool) -> list:
    """
    Filter, die NUR bei ddagrab nötig sind: dessen Bilder liegen im
    Grafikspeicher (D3D11) und müssen erst in den Hauptspeicher geholt
    werden, bevor libx264 & Co. sie kodieren können.
    """
    if not use_ddagrab:
        return []
    return ["-vf", "hwdownload,format=bgra"]


def build_video_input_args(
    mode_region: bool, region: tuple | None, fps: str,
    screen_size: tuple[int, int] | None = None,
    use_ddagrab: bool = False,
    wallclock_sync: bool = False,
) -> list:
    """
    Baut die Video-Eingabeparameter für den jeweiligen Screen-Grabber.

    Windows -> ddagrab (bevorzugt, GPU) oder gdigrab (Rückfallebene)
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
    # Gemeinsame Uhr mit der Tonquelle (siehe build_record_command)
    sync_args = ["-use_wallclock_as_timestamps", "1"] if wallclock_sync else []

    # ---------------- WINDOWS: ddagrab (bevorzugt) -----------------------
    if IS_WINDOWS and use_ddagrab:
        opts = [
            "output_idx=0",
            f"framerate={fps}",
            "draw_mouse=1",
        ]
        if mode_region and region:
            x, y, w, h = _sanitize_region(region)
            # Negative Offsets kann ddagrab nicht abbilden (seine Offsets
            # sind relativ zum jeweiligen Monitor, nicht zum virtuellen
            # Desktop) - solche Bereiche filtert build_record_command
            # vorher heraus und nimmt dann gdigrab.
            opts += [f"video_size={w}x{h}", f"offset_x={x}", f"offset_y={y}"]
        return [*sync_args, "-f", "lavfi", "-i", "ddagrab=" + ":".join(opts)]

    # ---------------- WINDOWS: gdigrab (Rückfallebene) -------------------
    if IS_WINDOWS:
        args += [
            *sync_args,
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
def build_audio_input_args(
    audio_device: str | None, wallclock_sync: bool = False, offset_ms: int = 0,
) -> list:
    """
    Baut die Audio-Eingabeparameter.

    Windows -> dshow  (-i audio="Mikrofon (Realtek)")
    Linux   -> pulse  (-i alsa_input.pci-0000_00_1f.3.analog-stereo)

    wallclock_sync: Zeitstempel = Ankunftszeit (gemeinsame Uhr mit dem
                    Bild, siehe build_record_command).
    offset_ms:      manuelle Feinabstimmung ("Ton-Versatz" in der GUI) -
                    positiv = Ton später, negativ = Ton früher. Gleicht
                    z. B. die Eigenverzögerung von Bluetooth-Headsets aus,
                    die kein Zeitstempel erfassen kann.
    """
    if not audio_device:
        return []

    common: list[str] = []
    if offset_ms:
        common += ["-itsoffset", f"{offset_ms / 1000:.3f}"]

    if IS_WINDOWS:
        sync_args: list[str] = []
        if wallclock_sync:
            sync_args = [
                "-use_wallclock_as_timestamps", "1",
                # Kleine Bloecke (100 ms statt Geraetestandard, oft 500 ms):
                # Der Zeitstempel eines Blocks ist seine Ankunftszeit, also
                # das ENDE des Blocks. _build_audio_sync_filters rechnet
                # zwar auf den Blockanfang zurueck, aber je kleiner der
                # Block, desto frueher beginnt der Ton in der Aufnahme und
                # desto kleiner wirkt sich Zeitversatz bei der Zustellung aus.
                "-audio_buffer_size", "100",
            ]
        return [
            *common,
            *sync_args,
            "-f", "dshow",
            "-thread_queue_size", "1024",
            # BEWUSST KEIN -audio_buffer_size mehr.
            #
            # Frueher stand hier 80 ms, dann von mir auf 500 ms erhoeht -
            # in der Annahme, ein ueberlaufender Puffer verursache die
            # gemeldete Tonverzerrung. Die Messung auf echter Hardware
            # hat das widerlegt: 500, 100, 50 ms und "gar nicht gesetzt"
            # verhielten sich praktisch identisch (11,2 / 11,0 / 11,6 /
            # 11,9 fps im Mittel). Der Wert war also nie die Ursache.
            # Ohne ausdrueckliche Angabe gilt der Geraetestandard - ein
            # Sonderwert weniger, der spaeter jemanden in die Irre
            # fuehren kann.
            # Realtime-Puffer von FFmpeg selbst - NICHT zu verwechseln mit
            # -audio_buffer_size, das den Puffer des Audiogeraets meint.
            # Standard sind nur ca. 3 MB; laeuft der ueber, verwirft FFmpeg
            # Audiopakete ("real-time buffer too full" - eine WARNUNG, die
            # bei -loglevel error unsichtbar blieb). Die Tonspur bekommt
            # dadurch Luecken oder endet vorzeitig, waehrend das Bild
            # normal weiterlaeuft - genau das gemeldete "Ton ist nur kurz".
            "-rtbufsize", "256M",
            "-i", f"audio={audio_device}",
        ]

    # Linux: PulseAudio / PipeWire (pipewire-pulse ist API-kompatibel)
    return [
        *common,
        "-f", "pulse",
        "-thread_queue_size", "1024",
        "-fragment_size", "1024",
        "-i", audio_device,
    ]


# ============================================================================
# 4) OUTPUT-ENCODING
# ============================================================================
def _build_audio_sync_filters() -> list[str]:
    """
    Filter für die Ton/Bild-Synchronisation unter Windows (nur zusammen mit
    wallclock_sync, siehe build_record_command).

    1) aselect - Ton-STAU zu Beginn verwerfen:
       DirectShow nimmt ab dem Öffnen der Tonquelle auf. FFmpeg liest die
       Tonpakete aber erst, wenn auch die Bildquelle bereit ist (ddagrab
       braucht dafür einige hundert ms). Die bis dahin aufgelaufenen Pakete
       kommen dann alle auf einmal an und tragen daher fast dieselbe
       Ankunftszeit - als Zeitstempel unbrauchbar. Ausgewählt wird deshalb
       erst ein Paket, das im normalen Takt ankommt (Abstand zum Vorgänger
       ~ eigene Blocklänge); ab dann alles. Nach spätestens 3 s wird in
       jedem Fall begonnen (Sicherung bei sehr unregelmäßiger Zustellung).

    2) asetpts - Ankunftszeit -> Aufnahmezeit:
       Ein Block kommt erst an, wenn er voll ist. Sein Zeitstempel zeigt
       also auf das ENDE der aufgenommenen Zeitspanne; die eigene Länge
       (NB_SAMPLES/SR) abziehen ergibt den Anfang.

    In einer Simulation mit 0,15-1,2 s Startverzögerung der Bildquelle und
    zufälligen Zustellverzögerungen lag der Rest-Versatz damit bei
    -25 bis +37 ms (vorher: Versatz = Startverzögerung, also mehrere
    hundert ms). Wahrnehmbar wird Versatz erst ab etwa 45 ms (Ton zu
    früh) bzw. 125 ms (Ton zu spät).
    """
    expr = (
        "if(isnan(prev_selected_t),"
        "gte(t-start_t,3)+between(t-prev_t,0.85*samples_n/sample_rate,1.15*samples_n/sample_rate),"
        "1)"
    )
    return [f"aselect=e='{expr}'", "asetpts=PTS-NB_SAMPLES/SR/TB"]


def _build_audio_filter_args(gain: float, denoise: bool, wallclock_sync: bool = False) -> list:
    """
    Baut die '-af'-Filterkette für die Mikrofon-Aufnahme:
      - aresample: haelt die Tonspur synchron (siehe unten) - IMMER aktiv
      - afftdn:    einfache Rauschunterdrückung (Wunsch: "Performance vom Mikro")
      - volume:    digitale Verstärkung/Abschwächung (Wunsch: "Lautstärke vom Mikro")
      - alimiter:  Sicherheitsnetz GEGEN digitales Clipping (siehe unten)
    """
    filters = _build_audio_sync_filters() if wallclock_sync else []
    # IMMER (nach den Sync-Filtern): haelt die Tonspur an der Zeitachse ausgerichtet und
    # fuellt Aussetzer der Aufnahmequelle mit Stille auf, statt die Spur
    # dort enden bzw. verrutschen zu lassen. Ohne das endet die Audiospur
    # bei einem kurzen Geraeteaussetzer schlicht vorzeitig, waehrend das
    # Bild weiterlaeuft ("Ton ist nur kurz", Datei aber volle Laenge).
    filters.append("aresample=async=1")
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


def build_video_encoder_args(encoder: str, preset: str, fps: str = "30") -> list:
    """
    Reine Video-Encoder-Parameter (Codec, Preset, Qualität, Pixelformat,
    Keyframe-Abstand) - OHNE Container-Optionen wie -movflags.

    Bewusst als eigene Funktion: Aufnahme (build_output_args) UND
    Leistungstest (build_benchmark_command) nutzen exakt diese Parameter.
    Früher hat der Test mit anderen Einstellungen kodiert als die echte
    Aufnahme (ohne -tune zerolatency) und dadurch Presets/Bildraten
    empfohlen, die bei der Aufnahme selbst nicht mehr hinterherkamen ->
    fehlende Bilder, ruckelige Videos.
    """
    gop = _gop_size(fps)

    if encoder in QSV_ENCODERS:
        # Intel Quick Sync kennt kein '-crf' und unterstützt die Presets
        # 'ultrafast'/'superfast' von libx264/265 nicht - auf 'veryfast'
        # abbilden statt einen FFmpeg-Fehler zu riskieren.
        qsv_preset = "veryfast" if preset in ("ultrafast", "superfast") else preset
        args = [
            "-c:v", encoder,
            "-preset", qsv_preset,
            "-global_quality", QSV_GLOBAL_QUALITY,
            "-pix_fmt", PIXEL_FORMAT,
            "-g", gop,
        ]
        if encoder == "hevc_qsv":
            args += ["-tag:v", "hvc1"]
        return args

    crf = CRF_X265 if encoder == "libx265" else CRF_X264
    # BEWUSST KEIN "-tune zerolatency" mehr.
    #
    # zerolatency ist fuer Livestreams gedacht (jedes Bild sofort raus).
    # Es schaltet x264/x265 u. a. auf "sliced threads" um - laut x264
    # selbst "Low-latency but lower-efficiency threading" - und deaktiviert
    # Lookahead und B-Frames. Fuer eine Aufnahme in eine DATEI bringt die
    # geringere Latenz nichts, kostet auf Mehrkern-CPUs aber spuerbar
    # Durchsatz (und Dateigroesse). Ohne das Tuning verteilt der Encoder
    # ganze Bilder auf die Kerne ("frame threads") und schafft dadurch
    # mehr Bilder pro Sekunde - genau das, was gegen Ruckeln hilft.
    args = [
        "-c:v", encoder,
        "-preset", preset,
        "-crf", crf,
        "-pix_fmt", PIXEL_FORMAT,
        "-g", gop,                     # Keyframe alle 2 s (an fps gekoppelt)
    ]
    if encoder == "libx265":
        args += ["-tag:v", "hvc1"]
    return args


def build_output_args(
    encoder: str, preset: str, has_audio: bool, audio_only: bool = False,
    gain: float = 1.0, denoise: bool = False, fps: str = "30",
    wallclock_sync: bool = False,
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
    audio_filter_args = (
        _build_audio_filter_args(gain, denoise, wallclock_sync) if has_audio else []
    )

    if audio_only:
        return [
            "-vn",  # explizit keine Video-Ausgabe
            "-c:a", AUDIO_CODEC,
            "-b:a", AUDIO_BITRATE,
            "-ar", AUDIO_SAMPLERATE,
            "-ac", "2",
            *audio_filter_args,
        ]

    args = build_video_encoder_args(encoder, preset, fps)
    args += ["-movflags", "+faststart"]     # MP4 sofort abspielbar

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
    audio_offset_ms: int = 0,
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

    audio_offset_ms: manuelle Ton-Verschiebung (nur bei Bild + Ton).

    TON/BILD-SYNCHRONISATION (Windows, Bild + Ton):
    Ohne Zusatzmaßnahmen setzt FFmpeg JEDE Eingabe für sich auf 0 - die
    erste Tonprobe und das erste Bild gelten als gleichzeitig. Die
    Tonquelle wird aber zuerst geöffnet und nimmt sofort auf, die
    Bildquelle (ddagrab) liefert ihr erstes Bild erst einige hundert ms
    später. Genau um diese Startverzögerung kam der Ton im Video zu spät
    - und weil sie von Start zu Start schwankt, war der Versatz nicht
    einmal konstant.

    Lösung: Beide Quellen bekommen dieselbe Uhr (Ankunftszeit beim
    Einlesen, -use_wallclock_as_timestamps), -copyts behält diese
    gemeinsame Zeitachse bei, und -avoid_negative_ts make_zero setzt
    erst die fertige Datei auf 0 - für alle Spuren um denselben Betrag.
    Die Tonspur braucht dafür noch zwei Korrekturen, siehe
    _build_audio_sync_filters.
    """
    wallclock_sync = IS_WINDOWS and bool(audio_device) and not audio_only

    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        # "warning" statt "error": die fuer Aufnahmeprobleme
        # entscheidenden Meldungen von dshow/x11grab ("real-time buffer
        # too full", verworfene Pakete, Geraeteaussetzer) sind WARNUNGEN.
        # Mit "error" blieben sie unsichtbar - das Diagnose-Log meldete
        # dann "keine Ausgabe erfasst", obwohl im Hintergrund gerade die
        # Tonspur zerfiel. Die Menge bleibt gering (keine
        # Fortschrittszeilen), der Ringpuffer in recorder.py haelt
        # ohnehin nur die letzten 40 Zeilen.
        "-loglevel", "warning",
        "-y",
    ]
    if wallclock_sync:
        cmd.append("-copyts")

    # REIHENFOLGE DER EINGABEN IST ENTSCHEIDEND - Audio MUSS zuerst stehen.
    #
    # Auf echter Windows-Hardware dreimal nachgemessen (je 15 s, sieben
    # Varianten, sonst voellig identische Parameter):
    #
    #   Bild zuerst, Ton danach   ->  7,7 - 12,7 fps von 30
    #   Ton zuerst, Bild danach   -> 29,8 - 30,0 fps von 30
    #
    # Weder Puffergroesse (500/100/50/keine) noch eine eigene
    # Warteschlange am Videoeingang aenderten etwas - nur die
    # Reihenfolge. Steht die ddagrab-Quelle als erste Eingabe, wird sie
    # offenbar im Gleichschritt mit dem langsam eintreffenden dshow-Ton
    # abgefragt und liefert entsprechend weniger Bilder.
    cmd += build_audio_input_args(
        audio_device, wallclock_sync=wallclock_sync,
        offset_ms=0 if audio_only else int(audio_offset_ms or 0),
    )

    use_ddagrab = False
    if not audio_only:
        use_ddagrab = _should_use_ddagrab(mode_region, region, screen_size)
        cmd += build_video_input_args(
            mode_region, region, fps, screen_size=screen_size,
            use_ddagrab=use_ddagrab, wallclock_sync=wallclock_sync,
        )

    has_audio = bool(audio_device) or audio_only
    # Muss VOR den Encoder-Optionen stehen: holt die GPU-Bilder von
    # ddagrab in den Hauptspeicher (bei gdigrab/x11grab leer).
    cmd += build_video_filter_args(use_ddagrab)
    cmd += build_output_args(
        encoder, preset, has_audio, audio_only=audio_only, gain=gain, denoise=denoise,
        fps=fps, wallclock_sync=wallclock_sync,
    )
    if wallclock_sync:
        # Gemeinsame Zeitachse erst in der Datei auf 0 setzen - fuer alle
        # Spuren um denselben Betrag, damit ihr Abstand erhalten bleibt.
        cmd += ["-avoid_negative_ts", "make_zero"]

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
    preset: str = "medium", fps: str = "30",
    screen_size: tuple[int, int] | None = None,
    realtime: bool = False, encoder: str = "libx264",
) -> list:
    """
    Ein Messlauf des Leistungstests.

    Kodiert bewegte Testbilder (testsrc2) in Bildschirmauflösung - mit
    GENAU den Encoder-Parametern der echten Aufnahme
    (build_video_encoder_args) und derselben Farbumrechnung: ddagrab
    liefert BGRA-Bilder, die vor dem Kodieren nach yuv420p umgerechnet
    werden müssen. Deshalb erzeugt testsrc2 hier ebenfalls BGRA.

    Warum eine synthetische Quelle statt einer Bildschirmaufnahme: sie
    liefert garantiert bei jedem Bild neuen Inhalt. Eine
    Bildschirmaufnahme misst mit, wie viel sich zufällig gerade auf dem
    Desktop bewegt - bei ruhigem Bildschirm sah selbst eine überforderte
    Maschine gut aus.

    realtime=True:  Bilder kommen im Takt der Bildrate (wie bei einer
                    echten Aufnahme) -> die CPU-Auslastung zeigt, was die
                    Aufnahme die Maschine kostet.
    realtime=False: so schnell wie möglich -> die Bilder pro Sekunde
                    zeigen den maximalen Durchsatz des Encoders.

    Fortschritt (Anzahl kodierter Bilder) kommt alle 0,5 s über stdout
    (-progress pipe:1). Der Lauf hat kein festes Ende - der Aufrufer
    beendet ihn nach der Messzeit; -t ist nur eine Sicherung, falls das
    einmal nicht klappt. Die Ausgabe geht ins Nichts (-f null).
    """
    width, height = screen_size if screen_size else (1920, 1080)
    # Gerade Kantenlaengen sind Pflicht fuer yuv420p
    width -= width % 2
    height -= height % 2

    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-stats_period", "0.5",
        "-y",
    ]
    if realtime:
        cmd.append("-re")
    cmd += [
        "-f", "lavfi",
        "-i", f"testsrc2=size={width}x{height}:rate={fps},format=bgra",
        "-t", "300",
    ]
    cmd += build_video_encoder_args(encoder, preset, fps)
    cmd += ["-an", "-f", "null", "-"]
    return cmd