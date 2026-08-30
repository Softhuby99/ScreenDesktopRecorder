"""
audio_devices.py
================
Erkennt verfügbare Audioquellen - cross-platform.

Linux:
  1. `pactl list short sources`  -> exakte PulseAudio-Sourcenamen (Pflicht für ffmpeg -f pulse)
  2. sounddevice                 -> Fallback

Windows:
  1. `ffmpeg -list_devices` (dshow) -> exakte DirectShow-Namen
  2. sounddevice                    -> Ergänzung

Die Rückgabe ist eine Liste von (anzeigename, ffmpeg_id)-Tupeln,
damit die GUI lesbare Namen zeigt, FFmpeg aber die exakte ID erhält.
"""

import re
import subprocess

from platform_utils import IS_LINUX, IS_WINDOWS, get_subprocess_flags


# ============================================================================
# 1) LINUX: PULSEAUDIO / PIPEWIRE
# ============================================================================
def list_pulse_sources() -> list[tuple[str, str]]:
    """
    Listet PulseAudio-Quellen via `pactl`.

    Monitor-Quellen ('.monitor') entsprechen dem Windows-'Stereomix'
    und nehmen den Systemton auf.

    :return: Liste von (Anzeigename, PulseAudio-Sourcename)
    """
    if not IS_LINUX:
        return []

    sources: list[tuple[str, str]] = []

    # --- Schritt 1: technische Namen holen ------------------------------
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    raw_names: list[str] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            raw_names.append(parts[1].strip())

    # --- Schritt 2: lesbare Beschreibungen zuordnen ----------------------
    descriptions = _get_pulse_descriptions()

    for name in raw_names:
        pretty = descriptions.get(name, name)
        # Monitor-Quellen klar kennzeichnen
        if name.endswith(".monitor"):
            pretty = f"🔊 Systemton: {pretty}"
        else:
            pretty = f"🎤 {pretty}"
        sources.append((pretty, name))

    return sources


def _get_pulse_descriptions() -> dict[str, str]:
    """
    Parst `pactl list sources` und mappt technische Namen
    auf menschenlesbare Beschreibungen.
    """
    mapping: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        current_name = None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Name:"):
                current_name = stripped.split("Name:", 1)[1].strip()
            elif stripped.startswith("Description:") and current_name:
                mapping[current_name] = stripped.split("Description:", 1)[1].strip()
                current_name = None
    except Exception:
        pass
    return mapping


# ============================================================================
# 2) WINDOWS: DIRECTSHOW
# ============================================================================
def list_dshow_audio_devices(timeout: int = 10) -> list[tuple[str, str]]:
    """
    Ruft `ffmpeg -list_devices true -f dshow -i dummy` auf und parst
    die Audio-Gerätenamen aus der stderr-Ausgabe.
    """
    if not IS_WINDOWS:
        return []

    try:
        from ffmpeg_utils import get_ffmpeg_path
        cmd = [
            get_ffmpeg_path(),
            "-hide_banner",
            "-list_devices", "true",
            "-f", "dshow",
            "-i", "dummy",
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **get_subprocess_flags(),
        )
        output = proc.stderr.decode("utf-8", errors="ignore")
    except Exception:
        return []

    devices: list[tuple[str, str]] = []
    in_audio_section = False

    for line in output.splitlines():
        low = line.lower()

        if "directshow audio devices" in low:
            in_audio_section = True
            continue
        if "directshow video devices" in low:
            in_audio_section = False
            continue

        match = re.search(r'"([^"]+)"', line)
        if not match:
            continue
        name = match.group(1)

        if name.startswith("@device"):
            continue

        if "(audio)" in low or (in_audio_section and "(video)" not in low):
            devices.append((f"🎤 {name}", name))

    # Duplikate entfernen
    seen = set()
    unique = []
    for display, ident in devices:
        if ident not in seen:
            seen.add(ident)
            unique.append((display, ident))
    return unique


# ============================================================================
# 3) SOUNDDEVICE-FALLBACK (beide Plattformen)
# ============================================================================
def list_sounddevice_inputs() -> list[tuple[str, str]]:
    """
    Listet Eingabegeräte via sounddevice.
    Fehler werden geschluckt, damit die App auch ohne PortAudio startet.
    """
    try:
        import sounddevice as sd
    except Exception:
        return []

    results: list[tuple[str, str]] = []
    try:
        for device in sd.query_devices():
            if device.get("max_input_channels", 0) > 0:
                name = str(device.get("name", "")).strip()
                if name:
                    results.append((f"🎤 {name}", name))
    except Exception:
        return []

    seen = set()
    unique = []
    for display, ident in results:
        if ident not in seen:
            seen.add(ident)
            unique.append((display, ident))
    return unique


# ============================================================================
# 4) ÖFFENTLICHE API
# ============================================================================
def get_audio_sources() -> list[tuple[str, str]]:
    """
    Liefert alle nutzbaren Audioquellen als (Anzeigename, FFmpeg-ID).

    Priorität: native Backend-Namen (pactl / dshow),
    da nur diese garantiert von FFmpeg akzeptiert werden.
    """
    if IS_LINUX:
        primary = list_pulse_sources()
    elif IS_WINDOWS:
        primary = list_dshow_audio_devices()
    else:
        primary = []

    if primary:
        return primary

    # Fallback, falls das native Tool fehlt
    return list_sounddevice_inputs()


def find_system_audio(sources: list[tuple[str, str]]) -> str | None:
    """
    Sucht eine Systemton-Loopback-Quelle.

    Linux:   '*.monitor'
    Windows: 'Stereomix' / 'Stereo Mix' / 'What U Hear'

    :return: FFmpeg-ID oder None
    """
    if IS_LINUX:
        for _display, ident in sources:
            if ident.endswith(".monitor"):
                return ident
        return None

    keywords = ["stereomix", "stereo mix", "stereo-mix",
                "what u hear", "wave out", "loopback", "summe"]
    for _display, ident in sources:
        low = ident.lower().replace(" ", "").replace("-", "")
        if any(kw.replace(" ", "").replace("-", "") in low for kw in keywords):
            return ident
    return None