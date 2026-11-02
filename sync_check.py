"""Generate an A/V reference and measure a recording of it (FFmpeg only)."""
import argparse
import re
import statistics
import subprocess
from ffmpeg_utils import get_ffmpeg_path
from platform_utils import get_subprocess_flags


def run(args):
    p = subprocess.run([get_ffmpeg_path(), '-hide_banner', '-nostdin', *args],
                       capture_output=True, text=True, encoding='utf-8', errors='replace',
                       **get_subprocess_flags())
    if p.returncode:
        raise RuntimeError(p.stderr[-3000:])
    return p.stderr


def generate(path, duration=30):
    if duration < 6:
        raise ValueError('Mindestens 6 Sekunden fuer zwei Messereignisse verwenden.')
    # Same event times in both sources: 2, 5, 8, ... seconds.
    gate = 'gte(t,2)*lt(mod(t-2,3),0.12)'
    run(['-n', '-f', 'lavfi', '-i', f'color=black:s=640x360:r=30:d={duration}',
         '-f', 'lavfi', '-i',
         f"aevalsrc=exprs='0.6*sin(2*PI*1000*t)*{gate}':s=48000:d={duration}",
         '-vf', f"drawbox=color=white:t=fill:enable='{gate}'",
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
         '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', path])


def measure(path, crop=None, threshold=-25):
    # Optional crop must describe just the reference video's display area.
    vf = (f'crop={crop},' if crop else '') + 'blackdetect=d=0.3:pix_th=0.25:pic_th=0.90'
    log = run(['-v', 'info', '-i', path, '-map', '0:v:0', '-map', '0:a:0',
               '-vf', vf, '-af', f'silencedetect=noise={threshold}dB:d=0.3',
               '-f', 'null', '-'])
    video = [float(t) for t in re.findall(r'black_end:([\d.]+)', log)]
    audio = [float(t) for t in re.findall(r'silence_end: ([\d.]+)', log)]
    # Both detectors close a final black/silent interval at EOF; it is not an event.
    duration = re.search(r'Duration: (\d+):(\d+):([\d.]+)', log)
    if not duration:
        raise ValueError('Dateidauer nicht auslesbar.')
    h, m, sec = map(float, duration.groups())
    end = h*3600 + m*60 + sec - 0.25
    video = [t for t in video if t < end]
    audio = [t for t in audio if t < end]
    if len(video) < 2 or len(video) != len(audio):
        raise ValueError(f'Keine eindeutige Zuordnung: {len(video)} Blitze, {len(audio)} Toene. '
                         'Nur den Testbildbereich aufnehmen/mit --crop begrenzen; '
                         'Hintergrundgeraeusche vermeiden oder --threshold anpassen.')
    # Do not report a confident offset when whole events were lost or extra sounds detected.
    if any(abs((b-a)-3) > 0.4 for events in (video, audio) for a,b in zip(events, events[1:])):
        raise ValueError('Unregelmaessige Ereignisabstaende; Messung nicht verlaesslich.')
    offsets = [(a-v)*1000 for v,a in zip(video,audio)]
    return video, audio, offsets


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='action', required=True)
    gen = sub.add_parser('generate', help='Testvideo mit synchronen Blitzen und Toenen erzeugen')
    gen.add_argument('path')
    gen.add_argument('--duration', type=float, default=30)
    check = sub.add_parser('measure', help='Aufnahme des Testvideos auswerten')
    check.add_argument('path')
    check.add_argument('--crop', help='Testbildbereich als Breite:Hoehe:X:Y')
    check.add_argument('--threshold', type=float, default=-25, help='Tonerkennungsschwelle in dB')
    args = ap.parse_args()
    try:
        if args.action == 'generate':
            generate(args.path, args.duration)
            print(f'Testvideo erzeugt: {args.path}')
        else:
            video, audio, offsets = measure(args.path, args.crop, args.threshold)
            print('Ereignis | Bild (s) | Ton (s) | Tonversatz (ms, positiv = spaeter)')
            for i,(v,a,d) in enumerate(zip(video,audio,offsets), 1):
                print(f'{i:8d} | {v:8.3f} | {a:7.3f} | {d:+.1f}')
            median = statistics.median(offsets)
            print(f'Median: {median:+.1f} ms; Aenderung erstes/letztes Ereignis: {offsets[-1]-offsets[0]:+.1f} ms')
            print(f'Bei konstantem Versatz: Ton-Regler um {-median:+.0f} ms gegenueber aktuellem Wert aendern.')
            print('Messaufloesung durch Bildrate begrenzt. Akustische Wiedergabe-/Mikrofonlatenz ist enthalten.')
            print('Nur gueltig, wenn alle Blitze und Toene der Testsequenz aufgenommen wurden.')
    except (ValueError, RuntimeError) as exc:
        ap.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
