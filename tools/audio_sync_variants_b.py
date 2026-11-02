"""
tools/audio_sync_variants_b.py (frueher test_audio_sync_b.py) - Runde B
=======================================================================
HISTORISCHES Diagnose-Skript (Basis: ohne wallclock/copyts): baut die
Varianten aus dem jeweils aktuellen Builder und reproduziert die damaligen
B-Befehle nicht mehr. Ausgabe in tools/messungen/.

Runde A hat gezeigt: -use_wallclock_as_timestamps + -copyts zerstoert das
Video (frame duplication too large). Ohne beides ist der Versatz besser,
aber nicht weg. Diese Runde variiert NUR Audio-Filter, Offset und Reihenfolge.

Varianten (alle ohne wallclock/copyts):
  B01_basis             - wie A02 (aselect + asetpts + aresample async=1)
  B02_nur_aresample     - Audio-Filter nur aresample=async=1
  B03_ohne_audiofilter  - gar kein -af
  B04_async1000         - aresample=async=1000:first_pts=0
  B05_asetpts_start     - asetpts=PTS-STARTPTS + aresample=async=1
  B06_audio_zuerst_off  - Basis, Audio um +150 ms verzoegert (itsoffset)
  B07_audio_frueh_off   - Basis, Audio um -150 ms (itsoffset)
  B08_vsync_passthrough - Basis, Video -fps_mode vfr statt cfr
  B09_kleiner_puffer    - Basis, rtbufsize 64M, thread_queue_size 512

Aufruf:
  python tools/audio_sync_variants_b.py "<Geraetename>"
Optional: --dauer 10 --fps 30 --encoder libx264 --preset veryfast --nur B03
"""

import argparse
import datetime
import os
import subprocess
import sys
import time

# Skriptordner (tools/) und Projektordner getrennt: die App-Module liegen
# im Projektordner, das Schwesterskript audio_sync_variants_a in tools/.
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
for _p in (PROJECT_DIR, SCRIPT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
MESS_DIR = os.path.join(SCRIPT_DIR, "messungen")

import ffmpeg_utils as fu  # noqa: E402
import audio_sync_variants_a as ta  # noqa: E402  (Hilfsfunktionen aus Runde A)


# ---------------------------------------------------------------------------
# Hilfsfunktionen fuer Kommando-Umbau
# ---------------------------------------------------------------------------
def get_arg(cmd, flag):
    """Wert hinter einem Flag (erstes Vorkommen) oder None."""
    if flag in cmd:
        i = cmd.index(flag)
        if i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def set_arg(cmd, flag, value):
    """Ersetzt den Wert eines Flags (alle Vorkommen)."""
    out, i = list(cmd), 0
    while i < len(out):
        if out[i] == flag and i + 1 < len(out):
            out[i + 1] = value
            i += 2
        else:
            i += 1
    return out


def drop_arg(cmd, flag):
    """Entfernt Flag samt Wert (alle Vorkommen)."""
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == flag:
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


def insert_before_audio_input(cmd, flag, value):
    """Setzt 'flag value' direkt vor das erste '-i' (= Audio-Eingang)."""
    i = cmd.index("-i")
    # Das erste -i hat davor seine Eingangsoptionen (-f dshow ...).
    # Wir setzen die Option ganz an den Anfang der Eingangsoptionen.
    start = cmd.index("-f") if "-f" in cmd else i
    return cmd[:start] + [flag, value] + cmd[start:]


def base_cmd(kw):
    """Basis: Runde A / 02 (ohne wallclock, copyts, avoid_negative_ts)."""
    return ta.strip_wallclock(fu.build_record_command(**kw))


# ---------------------------------------------------------------------------
# Varianten
# ---------------------------------------------------------------------------
VARIANTEN = [
    "B01_basis",
    "B02_nur_aresample",
    "B03_ohne_audiofilter",
    "B04_async1000",
    "B05_asetpts_start",
    "B06_audio_zuerst_off",
    "B07_audio_frueh_off",
    "B08_vsync_passthrough",
    "B09_kleiner_puffer",
]


def build_variant(name, base_kwargs, out_path):
    kw = dict(base_kwargs)
    kw["output_path"] = out_path
    cmd = base_cmd(kw)

    if name == "B01_basis":
        return cmd

    if name == "B02_nur_aresample":
        return set_arg(cmd, "-af", "aresample=async=1")

    if name == "B03_ohne_audiofilter":
        return drop_arg(cmd, "-af")

    if name == "B04_async1000":
        return set_arg(cmd, "-af", "aresample=async=1000:first_pts=0")

    if name == "B05_asetpts_start":
        return set_arg(cmd, "-af", "asetpts=PTS-STARTPTS,aresample=async=1")

    if name == "B06_audio_zuerst_off":
        return insert_before_audio_input(cmd, "-itsoffset", "0.150")

    if name == "B07_audio_frueh_off":
        return insert_before_audio_input(cmd, "-itsoffset", "-0.150")

    if name == "B08_vsync_passthrough":
        return drop_arg(set_arg(cmd, "-fps_mode", "vfr"), "-r")

    if name == "B09_kleiner_puffer":
        cmd = set_arg(cmd, "-rtbufsize", "64M")
        return set_arg(cmd, "-thread_queue_size", "512")

    raise ValueError(f"Unbekannte Variante: {name}")


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------
def main():
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
        print(ta.list_dshow_devices())
        print('\nAufruf: python tools/audio_sync_variants_b.py "<Geraetename>"')
        return 0

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = os.path.join(MESS_DIR, f"audio_sync_B_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "protokoll.txt")

    base_kwargs = dict(
        fps=args.fps, encoder=args.encoder, preset=args.preset,
        mode_region=False, region=None,
        audio_device=args.audio_device, audio_only=False,
        gain=1.0, denoise=False, screen_size=None, audio_offset_ms=0,
    )

    print("Tipp: waehrend jeder Aufnahme ein Video mit Klatschen/Sprache "
          "abspielen, damit man den Versatz hoert.\n")

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"test_audio_sync Runde B - {stamp}\n")
        log.write(f"Audiogeraet: {args.audio_device}\n")
        log.write(f"Dauer je Variante: {args.dauer}s, fps={args.fps}, "
                  f"encoder={args.encoder}, preset={args.preset}\n")
        log.write("=" * 70 + "\n\n")

        for name in VARIANTEN:
            if args.nur and args.nur not in name:
                continue
            out_path = os.path.join(out_dir, f"{name}.mp4")
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
            rc, err = ta.record(cmd, args.dauer)
            log.write(f"Exitcode: {rc}   Laufzeit: {time.time() - t0:.1f}s\n")
            if os.path.isfile(out_path):
                log.write(f"Datei: {os.path.basename(out_path)} "
                          f"({os.path.getsize(out_path)} Bytes)\n")
                log.write("Spuren: " + ta.probe_streams(out_path) + "\n")
            else:
                log.write("Datei: NICHT erzeugt\n")
            if err.strip():
                log.write("FFmpeg-Meldungen:\n")
                for line in err.strip().splitlines()[-40:]:
                    log.write("  " + line + "\n")
            log.write("\n" + "-" * 70 + "\n\n")
            log.flush()
            time.sleep(1.5)

    print(f"\nFertig. Ordner: {out_dir}")
    print(f"Protokoll:      {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
