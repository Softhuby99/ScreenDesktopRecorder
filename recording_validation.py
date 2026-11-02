"""Begrenzte Dekodier- und Spurprüfungen mit dem ohnehin mitgelieferten FFmpeg (kein ffprobe)."""
import os
import subprocess
from platform_utils import get_subprocess_flags

# Spur-Endeprüfung (check_track_ends)
TRACK_END_TOLERANCE_SECONDS = 2.0   # fester Startwert, Anpassung in Stufe 2
TRACK_END_MAX_OFFSET_MS = 500       # Ton-Versatz wird auf ±500 ms begrenzt
TRACK_END_TIMEOUT_SECONDS = 30      # je Spur

_TRACK_LABELS = {"v": "Bildspur", "a": "Tonspur"}


def validate_recording(path, ffmpeg, *, expect_video, expect_audio):
    """Liefert eine Fehlermeldung oder ''. Dekodiert je Pflichtspur ein Bild/einen Block."""
    if not os.path.isfile(path) or os.path.getsize(path) <= 1024:
        return "Die Aufnahmedatei fehlt oder ist leer."
    kinds = (["v"] if expect_video else []) + (["a"] if expect_audio else [])
    if not kinds:
        return "Keine zu pruefende Spur angegeben."
    # Spuren getrennt pruefen: ein legitimer positiver Ton-Versatz darf nicht
    # scheitern, weil eine kurze Bilddekodierung endet, bevor der Ton beginnt.
    for kind in kinds:
        label = "Bildspur" if kind == "v" else "Tonspur"
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", path,
               "-map", f"0:{kind}:0", f"-frames:{kind}", "1", "-nostats", "-f", "framemd5", "-"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=20,
                                    **get_subprocess_flags())
        except subprocess.TimeoutExpired:
            return f"Die Pruefung der {label} hat das Zeitlimit ueberschritten."
        except OSError as exc:
            return f"Die Aufnahme konnte nicht geprueft werden: {exc}"
        if result.returncode:
            return f"{label} fehlt oder ist nicht lesbar: " + result.stderr.strip()[-1200:]
        # Ein Pruefsummen-Eintrag belegt tatsaechlich dekodierte Daten, nicht nur einen Spurkopf.
        records = [line for line in result.stdout.splitlines()
                   if line and not line.startswith("#") and "," in line
                   and line.split(",", 1)[0].strip().isdigit()]
        if not records:
            return f"Die {label} enthaelt keine dekodierbaren Daten."
    return ""


# ============================================================================
# SPUR-ENDE PRÜFEN
# ============================================================================
def build_track_end_command(path: str, ffmpeg: str, kind: str) -> list:
    """
    Kommando, das eine Spur per Stream-Copy ins Nichts schreibt und dabei
    maschinenlesbaren Fortschritt auf stdout meldet. Bewusst OHNE -copyts:
    damit läge out_time_us immer bei 0; ohne liefert es das Spur-Ende
    inklusive Startversatz (mit ffmpeg 7.1.5 nachgemessen).
    """
    return [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
            "-i", path, "-map", f"0:{kind}:0", "-c", "copy",
            "-progress", "pipe:1", "-f", "null", "-"]


def parse_progress_end_us(text: str) -> int | None:
    """
    Liest aus einer -progress-Ausgabe das Spur-Ende in Mikrosekunden.

    Gewertet wird nur ein Block, der mit progress=end abschließt UND ein
    out_time_us > 0 enthält. Sonst None ("Endzeit nicht ermittelbar").
    """
    block: dict[str, str] = {}
    result = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        block[key] = value
        if key != "progress":
            continue
        if value == "end":
            try:
                us = int(block.get("out_time_us", ""))
            except ValueError:
                us = 0
            result = us if us > 0 else None
        block = {}
    return result


def evaluate_track_end(kind: str, end_seconds: float, capture_seconds: float,
                       audio_offset_ms: int = 0) -> str:
    """
    Bewertet ein Spur-Ende gegen die Erfassungsdauer.

    Zu kurz, wenn Spur_Ende < Erfassungsdauer - (2,0 s + |Ton-Versatz|);
    der Versatz wird auf ±500 ms begrenzt. Ohne Erfassungsdauer (<= 0)
    wird nicht bewertet.
    :return: deutsche Warnung oder ''
    """
    if not capture_seconds or capture_seconds <= 0:
        return ""
    try:
        offset_ms = int(audio_offset_ms or 0)
    except (TypeError, ValueError):
        offset_ms = 0
    offset_ms = max(-TRACK_END_MAX_OFFSET_MS, min(TRACK_END_MAX_OFFSET_MS, offset_ms))
    tolerance = TRACK_END_TOLERANCE_SECONDS + abs(offset_ms) / 1000.0
    if end_seconds < capture_seconds - tolerance:
        label = _TRACK_LABELS.get(kind, "Spur")
        return (f"Die {label} endet bereits nach {end_seconds:.1f} s, die Aufnahme "
                f"lief aber {capture_seconds:.1f} s - die {label} ist vorzeitig abgebrochen.")
    return ""


def check_track_ends(path, ffmpeg, *, expect_video, expect_audio, capture_seconds,
                     audio_offset_ms=0, on_process=None, cancelled=None, log=None):
    """
    Prüft, ob erwartete Spuren vorzeitig enden. Liefert eine Liste deutscher
    Warnungen (leer = unauffällig). Warnungen machen eine Aufnahme nie
    ungültig - sie werden nur protokolliert und angezeigt.

    :param on_process: Callback(Popen) - meldet pro Spur den Prozess VOR dem
                       Lesen (damit DiagnosisThread.cancel() ihn beenden kann)
    :param cancelled:  Callable -> bool, vor jeder Spur geprüft
    :param log:        Callback(text) für Protokolleinträge
    """
    def note(text):
        if log is not None:
            try:
                log(text)
            except Exception:
                pass

    warnings: list[str] = []
    if not capture_seconds or capture_seconds <= 0:
        note("Spur-Endeprüfung übersprungen: keine Erfassungsdauer.")
        return warnings

    kinds = (["v"] if expect_video else []) + (["a"] if expect_audio else [])
    for kind in kinds:
        if cancelled is not None and cancelled():
            break
        label = _TRACK_LABELS[kind]
        cmd = build_track_end_command(path, ffmpeg, kind)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    **get_subprocess_flags())
        except OSError as exc:
            note(f"{label}: Endzeit nicht ermittelbar ({exc}).")
            continue
        try:
            if on_process is not None:
                on_process(proc)
            try:
                out, _err = proc.communicate(timeout=TRACK_END_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.communicate()
                except Exception:
                    pass
                note(f"{label}: Endzeit nicht ermittelbar (Zeitlimit "
                     f"{TRACK_END_TIMEOUT_SECONDS} s überschritten).")
                continue
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

        if cancelled is not None and cancelled():
            break
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="ignore")
        end_us = parse_progress_end_us(out or "")
        if end_us is None:
            note(f"{label}: Endzeit nicht ermittelbar.")
            continue
        end_seconds = end_us / 1_000_000
        note(f"{label}: endet bei {end_seconds:.3f} s (Erfassungsdauer {capture_seconds:.1f} s)")
        # Der Ton-Versatz verschiebt Bild- und Tonspur gegeneinander und
        # zählt deshalb für beide Spuren als zusätzliche Toleranz.
        warning = evaluate_track_end(kind, end_seconds, capture_seconds, audio_offset_ms)
        if warning:
            warnings.append(warning)
    return warnings
