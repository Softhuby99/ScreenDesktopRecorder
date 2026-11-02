import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ffmpeg_utils as fu
from recording_validation import validate_recording


class Commands(unittest.TestCase):
    def command(self, **kw):
        args = dict(output_path='out.mp4', fps='30', encoder='libx264', preset='veryfast',
                    audio_device='Microphone', screen_size=(640, 480))
        args.update(kw)
        with patch.object(fu, 'IS_WINDOWS', True), patch.object(fu, 'get_ffmpeg_path', return_value='ffmpeg'), patch.object(fu, '_should_use_ddagrab', return_value=True):
            return fu.build_record_command(**args)

    def test_no_arrival_time_or_epoch_or_block_removal(self):
        cmd = self.command()
        for option in ('-copyts', '-use_wallclock_as_timestamps', '-isync'):
            self.assertNotIn(option, cmd)
        self.assertEqual(cmd[cmd.index('-af')+1], 'aresample=async=1')
        self.assertLess(cmd.index('dshow'), cmd.index('lavfi'))
        self.assertIn('1:v:0', cmd)
        self.assertIn('0:a:0', cmd)

    def test_offsets_both_signs(self):
        for value in (-150, 150):
            cmd = self.command(audio_offset_ms=value)
            self.assertEqual(cmd[cmd.index('-itsoffset')+1], f'{value/1000:.3f}')
            self.assertLess(cmd.index('-itsoffset'), cmd.index('-i'))

    def test_audio_only_and_silent_video(self):
        audio = self.command(audio_only=True, audio_offset_ms=150)
        self.assertNotIn('-itsoffset', audio)
        self.assertNotIn('-fps_mode', audio)
        self.assertNotIn('lavfi', audio)
        video = self.command(audio_device=None)
        self.assertIn('0:v:0', video)
        self.assertNotIn('-af', video)
        with self.assertRaises(ValueError):
            self.command(audio_only=True, audio_device=None)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class RealFFmpeg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ffmpeg = shutil.which('ffmpeg')

    def run_ffmpeg(self, args):
        p = subprocess.run([self.ffmpeg, '-hide_banner', '-v', 'error', '-y', *args],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def test_audio_filter_preserves_first_block_and_sample_count(self):
        # Nonperiodic signal detects dropping/replacing the first block too.
        source = 'aevalsrc=t:s=48000:d=0.5,asetnsamples=n=4800:p=0'
        data = []
        for af in ('anull', fu._build_audio_filter_args(1, False)[1]):
            p = subprocess.run([self.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', source,
                                '-af', af, '-f', 's16le', '-'], capture_output=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            data.append(p.stdout)
        self.assertEqual(len(data[0]), 48000)
        self.assertEqual(data[0], data[1])

    def test_missing_video_detected_despite_successful_encode(self):
        path = str(Path(self.tmp.name)/'audio.mp4')
        self.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=duration=0.5', '-c:a', 'aac', path])
        self.assertGreater(Path(path).stat().st_size, 1024)
        self.assertTrue(validate_recording(path, self.ffmpeg, expect_video=True, expect_audio=True))
        self.assertEqual(validate_recording(path, self.ffmpeg, expect_video=False, expect_audio=True), '')

    def test_valid_av_and_missing_audio(self):
        for audio in (False, True):
            path = str(Path(self.tmp.name)/f'av{audio}.mp4')
            inputs = ['-f','lavfi','-i','testsrc2=size=64x64:rate=30:duration=0.5']
            if audio:
                inputs += ['-f','lavfi','-i','sine=duration=0.5']
            self.run_ffmpeg(inputs + fu.build_output_args('libx264','ultrafast',audio) + [path])
            self.assertEqual(validate_recording(path,self.ffmpeg,expect_video=True,expect_audio=audio), '')
            if not audio:
                self.assertTrue(validate_recording(path,self.ffmpeg,expect_video=True,expect_audio=True))

    def test_measure_reference_and_known_delay(self):
        from sync_check import generate, measure
        ref = str(Path(self.tmp.name)/'reference.mp4')
        delayed = str(Path(self.tmp.name)/'delayed.mp4')
        generate(ref, 6)
        _, _, offsets = measure(ref)
        self.assertEqual(len(offsets), 2)
        for offset in offsets:
            self.assertLess(abs(offset), 34)
        self.run_ffmpeg(['-i', ref, '-c:v', 'copy', '-af', 'adelay=150:all=1',
                         '-c:a', 'aac', delayed])
        _, _, offsets = measure(delayed)
        for offset in offsets:
            self.assertAlmostEqual(offset, 150, delta=34)

    def test_validation_accepts_late_audio(self):
        path = str(Path(self.tmp.name)/'late.mp4')
        self.run_ffmpeg(['-f','lavfi','-i','testsrc2=size=64x64:rate=30:duration=1',
                         '-itsoffset','0.5','-f','lavfi','-i','sine=duration=0.5',
                         '-c:v','libx264','-preset','ultrafast','-c:a','aac',path])
        self.assertEqual(validate_recording(path,self.ffmpeg,expect_video=True,expect_audio=True), '')

    def test_broken_file(self):
        path = Path(self.tmp.name)/'broken.mp4'
        path.write_bytes(b'x'*4096)
        self.assertTrue(validate_recording(str(path),self.ffmpeg,expect_video=True,expect_audio=True))

class RecorderFailures(unittest.TestCase):
    def test_fatal_warning_survives_ring_buffer(self):
        import io
        from types import SimpleNamespace
        from recorder import RecorderThread
        recorder = RecorderThread({'output_path': 'unused.mp4'})
        recorder._process = SimpleNamespace(stderr=io.BytesIO(
            b'510000 frame duplication too large, skipping\n' + b'later warning\n'*50))
        recorder._drain_stderr()
        self.assertEqual(len(recorder._stderr_buffer), 40)
        self.assertFalse(recorder._verify_output())
        self.assertIn('frame duplication too large', recorder.error_message)

    def test_nonzero_exit_is_not_success(self):
        from types import SimpleNamespace
        from recorder import RecorderThread
        recorder = RecorderThread({'output_path': 'unused.mp4'})
        recorder._process = SimpleNamespace(returncode=1)
        self.assertFalse(recorder._verify_output())
        self.assertIn('Code 1', recorder.error_message)


if __name__ == '__main__':
    unittest.main()
