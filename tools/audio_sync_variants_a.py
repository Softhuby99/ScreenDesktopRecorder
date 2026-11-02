"""
tools/audio_sync_variants_a.py (frueher test_audio_sync.py) - Variante A
========================================================================
HISTORISCHES Diagnose-Skript: baut die Varianten aus dem jeweils aktuellen
Builder und reproduziert die damaligen A-Befehle nicht mehr. Ausgabe in
tools/messungen/.

Nimmt pro Variante 10 s auf (Bild + Ton) und speichert je eine MP4 plus
ein Protokoll der FFmpeg-Befehle. Danach die MP4s anhoeren und vergleichen.

Varianten:
  01_aktuell            - Code unveraendert (wallclock + aselect + copyts)
  02_ohne_wallclock     - kein -use_wallclock_as_timestamps / -copyts
  03_ohne_aselect       - wallclock an, aber ohne aselect (nur asetpts)
  04_offset_minus200    - aktuell, audio_offset_ms = -200
  05_offset_0           - aktuell, audio_offset_ms = 0 (Referenz)
  06_offset_plus200     - aktuell, audio_offset_ms = +200
  07_nur_systemton      - nur Audio, kein Bild (audio_only)
  08_avoid_neg_einmal   - -avoid_negative_ts nur einmal (am Ende)

Aufruf (im Projektordner):
  python tools/audio_sync_variants_a.py "Stereo Mix (Realtek)"
  python tools/audio_sync_variants_a.py    -> listet Geraete und beendet sich
Optional: --dauer 10  --fps 30  --encoder libx264  --preset veryfast
"""

import argparse
import datetime
import os
import subprocess
import sys
import time

# Skriptordner (tools/) und Projektordner getrennt: die App-Module
# (ffmpeg_utils, platform_utils) liegen im Projektordner.
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
for _p in (PROJECT_DIR, SCRIPT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
MESS_DIR = os.path.join(SCRIPT_DIR, "messungen")

import ffmpeg_utils as fu  # noqa: E402

try:
    from platform_utils import get_subprocess_flags  # noqa: E402
except Exception:  # pragma: no cover
    def get_subprocess_flags():
        return {}


# ---------------------------------------------------------------------------
# Geraeteliste
# ---------------------------------------------------------------------------
def list_dshow_devices() -> str:
    cmd = [fu.get_ffmpeg_path(), "-hide_banner", "-list_devices", "true",
           "-f", "dshow", "-i", "dummy"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stderr or r.stdout


# ---------------------------------------------------------------------------
# Kommando-Manipulation (ohne ffmpeg_utils.py zu aendern)
# ---------------------------------------------------------------------------
def strip_wallclock(cmd: list) -> list:
    """Entfernt wallclock-Timestamps, -copyts und -avoid_negative_ts."""
    out, i = [], 0
    while i < len(cmd):
        a = cmd[i]
        if a == "-use_wallclock_as_timestamps":
            i += 2
            continue
        if a == "-copyts":
            i += 1
            continue
        if a == "-avoid_negative_ts":
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def count_avoid_negative_ts(cmd: list) -> int:
    return sum(1 for a in cmd if a == "-avoid_negative_ts")


class PatchSyncFilters:
    """Ersetzt _build_audio_sync_filters temporaer (z. B. ohne aselect)."""

    def __init__(self, replacement):
        self.replacement = replacement
        self.original = None

    def __enter__(self):
        self.original = fu._build_audio_sync_filters
        fu._build_audio_sync_filters = self.replacement

    def __exit__(self, *exc):
        fu._build_audio_sync_filters = self.original


def filters_ohne_aselect() -> list:
    return ["asetpts=PTS-NB_SAMPLES/SR/TB"]


# ---------------------------------------------------------------------------
# Varianten
# ---------------------------------------------------------------------------
def build_variant(name: str, base_kwargs: dict, out_path: str) -> list:
    kw = dict(base_kwargs)
    kw["output_path"] = out_path

    if name == "01_aktuell":
        return fu.build_record_command(**kw)

    if name == "02_ohne_wallclock":
        return strip_wallclock(fu.build_record_command(**kw))

    if name == "03_ohne_aselect":
        with PatchSyncFilters(filters_ohne_aselect):
            return fu.build_record_command(**kw)

    if name == "04_offset_minus200":
        kw["audio_offset_ms"] = -200
        return fu.build_record_command(**kw)

    if name == "05_offset_0":
        kw["audio_offset_ms"] = 0
        return fu.build_record_command(**kw)

    if name == "06_offset_plus200":
        kw["audio_offset_ms"] = 200
        return fu.build_record_command(**kw)

    if name == "07_nur_systemton":
        kw["audio_only"] = True
        return fu.build_record_command(**kw)

    if name == "08_avoid_neg_einmal":
        cmd = fu.build_record_command(**kw)
        # Falls mehrfach vorhanden: nur das letzte behalten.
        n = count_avoid_negative_ts(cmd)
        while n > 1:
            idx = cmd.index("-avoid_negative_ts")
            del cmd[idx:idx + 2]
            n -= 1
        return cmd

    raise ValueError(f"Unbekannte Variante: {name}")


VARIANTEN = [
    "01_aktuell",
    "02_ohne_wallclock",
    "03_ohne_aselect",
    "04_offset_minus200",
    "05_offset_0",
    "06_offset_plus200",
    "07_nur_systemton",
    "08_avoid_neg_einmal",
]


# ---------------------------------------------------------------------------
# Aufnahme
# ---------------------------------------------------------------------------
def record(cmd: list, dauer: float) -> tuple[int, str]:
    """Startet FFmpeg, beendet nach 'dauer' Sekunden sauber per 'q'."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, **get_subprocess_flags(),
    )
    time.sleep(dauer)
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        _, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return proc.returncode, (err or b"").decode("utf-8", errors="replace")


def probe_streams(path: str) -> str:
    """Kurzinfo ueber Dauer der Spuren (ffprobe, falls vorhanden)."""
    ffprobe = os.path.join(os.path.dirname(fu.get_ffmpeg_path()),
                           "ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not os.path.isfile(ffprobe):
        return "(ffprobe nicht gefunden - Spurdauer nicht geprueft)"
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,start_time,duration", "-of", "compact", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or r.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio_device", nargs="?", default=None)
    ap.add_argument("--dauer", type=float, default=10.0)
    ap.add_argument("--fps", default="30")
    ap.add_argument("--encoder", default="libx264")
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--nur", default=None,
                    help="nur Varianten, deren Name diesen Text enthaelt")
    args = ap.parse_args()

    if not args.audio_device:
        print("Kein Audiogeraet angegeben. Verfuegbare dshow-Geraete:\n")
        print(list_dshow_devices())
        print('\nAufruf: python tools/audio_sync_variants_a.py "<Geraetename>"')
        return 0

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = os.path.join(MESS_DIR, f"audio_sync_A_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "protokoll.txt")

    base_kwargs = dict(
        fps=args.fps, encoder=args.encoder, preset=args.preset,
        mode_region=False, region=None,
        audio_device=args.audio_device, audio_only=False,
        gain=1.0, denoise=False, screen_size=None, audio_offset_ms=0,
    )

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"test_audio_sync Variante A - {stamp}\n")
        log.write(f"Audiogeraet: {args.audio_device}\n")
        log.write(f"Dauer je Variante: {args.dauer}s, fps={args.fps}, "
                  f"encoder={args.encoder}, preset={args.preset}\n")
        log.write("=" * 70 + "\n\n")

        for name in VARIANTEN:
            if args.nur and args.nur not in name:
                continue
            ext = "m4a" if name == "07_nur_systemton" else "mp4"
            out_path = os.path.join(out_dir, f"{name}.{ext}")
            print(f"--> {name} ({args.dauer}s) ...", flush=True)
            try:
                cmd = build_variant(name, base_kwargs, out_path)
            except Exception as e:  # noqa: BLE001
                msg = f"FEHLER beim Bauen von {name}: {e}"
                print(msg)
                log.write(msg + "\n\n")
                continue

            log.write(f"[{name}]\n")
            log.write("Befehl:\n  " + subprocess.list2cmdline(cmd) + "\n")
            t0 = time.time()
            rc, err = record(cmd, args.dauer)
            log.write(f"Exitcode: {rc}   Laufzeit: {time.time() - t0:.1f}s\n")
            if os.path.isfile(out_path):
                log.write(f"Datei: {os.path.basename(out_path)} "
                          f"({os.path.getsize(out_path)} Bytes)\n")
                log.write("Spuren: " + probe_streams(out_path) + "\n")
            else:
                log.write("Datei: NICHT erzeugt\n")
            if err.strip():
                log.write("FFmpeg-Meldungen:\n")
                for line in err.strip().splitlines()[-40:]:
                    log.write("  " + line + "\n")
            log.write("\n" + "-" * 70 + "\n\n")
            log.flush()
            time.sleep(1.5)  # Geraet freigeben

    print(f"\nFertig. Ordner: {out_dir}")
    print(f"Protokoll:      {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
