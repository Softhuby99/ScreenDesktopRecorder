"""Aufnahmeprüfung: Erfassungsdauer, Spur-Endeprüfung, Diagnose, Stille-Hinweis."""
import sys; from pathlib import Path  # noqa: E702
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest.mock import Mock, patch  # noqa: E402

import diagnosis  # noqa: E402
import gui_logic  # noqa: E402
import recorder  # noqa: E402
import recording_validation as rv  # noqa: E402
from fake_processes import FakeClock, FakePsProcess, PopenFactory, progress_output  # noqa: E402

# Messwerte (ffmpeg 7.1.5): Datei 20 s Video / 5 s Ton
FIXTURE_AUDIO_5S = progress_output(5_000_000)
FIXTURE_VIDEO_20S = progress_output(20_000_000)
# Datei mit Audio -itsoffset 0.5 (Audio-Start 0,476 s, Dauer 19,023 s)
FIXTURE_AUDIO_OFFSET = progress_output(19_516_372)


def track_output(video: bytes, audio: bytes):
    """stdout je nach gemappter Spur (0:v:0 / 0:a:0)."""
    def pick(cmd):
        return video if "0:v:0" in cmd else audio
    return pick


# ============================================================================
# Parser und Bewertung (reine Funktionen, ohne ffmpeg)
# ============================================================================
class ProgressParser(unittest.TestCase):
    def test_fixtures(self):
        self.assertEqual(rv.parse_progress_end_us(FIXTURE_AUDIO_5S.decode()), 5_000_000)
        self.assertEqual(rv.parse_progress_end_us(FIXTURE_VIDEO_20S.decode()), 20_000_000)
        self.assertEqual(rv.parse_progress_end_us(FIXTURE_AUDIO_OFFSET.decode()), 19_516_372)

    def test_no_end_block_is_not_determinable(self):
        self.assertIsNone(rv.parse_progress_end_us(progress_output(5_000_000, end=False).decode()))
        self.assertIsNone(rv.parse_progress_end_us(""))

    def test_zero_or_missing_out_time_is_not_determinable(self):
        self.assertIsNone(rv.parse_progress_end_us(progress_output(0).decode()))
        self.assertIsNone(rv.parse_progress_end_us("out_time_us=N/A\nprogress=end\n"))
        self.assertIsNone(rv.parse_progress_end_us("progress=end\n"))

    def test_command_has_no_copyts(self):
        for kind in ("a", "v"):
            cmd = rv.build_track_end_command("x.mp4", "ffmpeg", kind)
            self.assertNotIn("-copyts", cmd)
            self.assertEqual(cmd[cmd.index("-map") + 1], f"0:{kind}:0")
            for flag in ("-nostdin", "-nostats", "-progress", "pipe:1", "-c", "copy", "null"):
                self.assertIn(flag, cmd)


class TrackEndEvaluation(unittest.TestCase):
    def test_boundaries_without_offset(self):
        self.assertEqual(rv.evaluate_track_end("a", 18.0, 20.0), "")
        self.assertIn("Tonspur", rv.evaluate_track_end("a", 17.99, 20.0))
        self.assertIn("Bildspur", rv.evaluate_track_end("v", 5.0, 20.0))

    def test_offset_plus_minus_500(self):
        for offset in (500, -500):
            self.assertEqual(rv.evaluate_track_end("a", 17.5, 20.0, offset), "")
            self.assertTrue(rv.evaluate_track_end("a", 17.49, 20.0, offset))

    def test_offset_is_clamped(self):
        self.assertTrue(rv.evaluate_track_end("a", 17.0, 20.0, 3000))
        self.assertEqual(rv.evaluate_track_end("a", 17.5, 20.0, -3000), "")

    def test_start_offset_not_counted_as_short(self):
        # Startzeit ≠ 0: Spur-Ende inkl. Startversatz
        self.assertEqual(rv.evaluate_track_end("a", 19.516372, 20.0, 500), "")

    def test_missing_capture_seconds_skips(self):
        self.assertEqual(rv.evaluate_track_end("a", 1.0, 0.0), "")
        self.assertEqual(rv.evaluate_track_end("a", 1.0, None), "")

    def test_message_is_german(self):
        msg = rv.evaluate_track_end("a", 5.0, 20.0)
        self.assertIn("vorzeitig", msg)
        self.assertIn("5.0 s", msg)


# ============================================================================
# check_track_ends mit Fake-Popen
# ============================================================================
class CheckTrackEnds(unittest.TestCase):
    def run_check(self, factory, **kw):
        log = []
        args = dict(expect_video=True, expect_audio=True, capture_seconds=20.0, log=log.append)
        args.update(kw)
        with patch("recording_validation.subprocess.Popen", factory):
            warnings = rv.check_track_ends("datei.mp4", "ffmpeg", **args)
        return warnings, log

    def test_short_audio_detected(self):
        factory = PopenFactory(stdout=track_output(FIXTURE_VIDEO_20S, FIXTURE_AUDIO_5S))
        warnings, log = self.run_check(factory)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Tonspur", warnings[0])
        self.assertEqual(len(factory.instances), 2)
        for proc in factory.instances:
            self.assertNotIn("-copyts", proc.cmd)
            self.assertIs(proc.kwargs["stderr"], subprocess.DEVNULL)
            self.assertIs(proc.kwargs["stdout"], subprocess.PIPE)

    def test_offset_file_is_ok(self):
        factory = PopenFactory(stdout=track_output(FIXTURE_VIDEO_20S, FIXTURE_AUDIO_OFFSET))
        warnings, _ = self.run_check(factory, audio_offset_ms=500)
        self.assertEqual(warnings, [])

    def test_audio_only_has_no_video_entry(self):
        factory = PopenFactory(stdout=track_output(FIXTURE_VIDEO_20S, FIXTURE_AUDIO_5S))
        warnings, log = self.run_check(factory, expect_video=False)
        self.assertEqual(len(factory.instances), 1)
        self.assertIn("0:a:0", factory.instances[0].cmd)
        self.assertFalse(any("Bildspur" in line for line in log + warnings))

    def test_not_determinable_is_logged_not_warned(self):
        factory = PopenFactory(stdout=progress_output(0))
        warnings, log = self.run_check(factory)
        self.assertEqual(warnings, [])
        self.assertEqual(sum("nicht ermittelbar" in line for line in log), 2)

    def test_timeout_logs_and_does_not_warn(self):
        factory = PopenFactory(stdout=FIXTURE_AUDIO_5S, timeout_first_communicate=True)
        warnings, log = self.run_check(factory, expect_video=False)
        self.assertEqual(warnings, [])
        self.assertTrue(factory.instances[0].killed)
        self.assertTrue(any("Zeitlimit" in line for line in log))

    def test_missing_capture_skips_everything(self):
        factory = PopenFactory(stdout=FIXTURE_AUDIO_5S)
        warnings, log = self.run_check(factory, capture_seconds=0.0)
        self.assertEqual(warnings, [])
        self.assertEqual(factory.instances, [])
        self.assertTrue(log)

    def test_cancel_during_check_kills_and_second_track_not_started(self):
        d = diagnosis.DiagnosisThread("datei.mp4", capture_seconds=20, ffmpeg="ffmpeg")
        factory = PopenFactory(stdout=FIXTURE_VIDEO_20S, on_communicate=lambda proc: d.cancel())
        warnings, _ = self.run_check(factory, on_process=d._publish_process,
                                     cancelled=lambda: d.cancelled)
        self.assertEqual(warnings, [])
        self.assertEqual(len(factory.instances), 1)
        self.assertTrue(factory.instances[0].killed)


# ============================================================================
# 4a Erfassungsdauer
# ============================================================================
class CaptureDuration(unittest.TestCase):
    def make(self):
        rec = recorder.RecorderThread({"output_path": "unbenutzt.mp4"})
        rec._clock = FakeClock()
        rec._process = PopenFactory()(["ffmpeg"])
        rec._ps_process = FakePsProcess()
        return rec

    def test_default_is_float_zero(self):
        rec = recorder.RecorderThread({"output_path": "x.mp4"})
        self.assertIsInstance(rec.get_capture_seconds(), float)
        self.assertEqual(rec.get_capture_seconds(), 0.0)

    def test_constant_after_freeze_and_second_call_changes_nothing(self):
        rec = self.make()
        rec._start_time = rec._clock()
        rec._clock.advance(60)
        rec._freeze_capture_seconds()
        rec._clock.advance(40)                 # Finalisierung
        rec._freeze_capture_seconds()          # zweiter Aufruf
        rec.stop()                             # stop() idempotent
        self.assertEqual(rec.get_capture_seconds(), 60.0)
        self.assertEqual(rec.get_elapsed(), 60.0)

    def test_pauses_are_subtracted(self):
        rec = self.make()
        rec._start_time = rec._clock()
        rec._clock.advance(10)
        self.assertTrue(rec.toggle_pause())
        rec._clock.advance(5)
        self.assertFalse(rec.toggle_pause())
        rec._clock.advance(10)
        rec.stop()
        self.assertEqual(rec.get_capture_seconds(), 20.0)

    def test_pause_running_at_stop_is_subtracted(self):
        rec = self.make()
        rec._start_time = rec._clock()
        rec._clock.advance(10)
        rec.toggle_pause()
        rec._clock.advance(30)
        rec.stop()
        self.assertEqual(rec.get_capture_seconds(), 10.0)

    def _run_patched(self, rec, build=None, factory=None):
        factory = factory or PopenFactory()
        with patch("recorder.subprocess.Popen", factory), \
                patch("recorder.psutil.Process", return_value=FakePsProcess()), \
                patch("recorder.build_record_command",
                      build or Mock(return_value=["ffmpeg", "out.mp4"])), \
                patch("recorder.validate_recording", return_value=""), \
                patch("recorder.get_ffmpeg_path", return_value="ffmpeg"), \
                patch("recorder.START_CHECK_SECONDS", 0):
            rec.run()
        return factory

    def test_ffmpeg_ending_by_itself_sets_capture(self):
        rec = recorder.RecorderThread({"output_path": "x.mp4", "fps": 30,
                                       "encoder": "libx264", "preset": "faster"})
        clock = FakeClock()
        rec._clock = clock

        def started():
            clock.advance(30)
            rec._process.returncode = 0        # FFmpeg endet von selbst

        rec.on_started = started
        self._run_patched(rec)
        self.assertTrue(rec._capture_frozen)
        self.assertEqual(rec.get_capture_seconds(), 30.0)

    def test_build_failure_gives_zero_and_callbacks(self):
        on_stopped = Mock()
        on_error = Mock()
        rec = recorder.RecorderThread({"output_path": "x.mp4"},
                                      on_stopped=on_stopped, on_error=on_error)
        self._run_patched(rec, build=Mock(side_effect=RuntimeError("kaputt")))
        self.assertEqual(rec.get_capture_seconds(), 0.0)
        self.assertIsInstance(rec.get_capture_seconds(), float)
        on_error.assert_called_once()
        on_stopped.assert_called_once_with("x.mp4", False)

    def test_60s_plus_40s_finalization_not_reported(self):
        rec = self.make()
        rec._start_time = rec._clock()
        rec._clock.advance(60)
        rec.stop()
        rec._clock.advance(40)                 # Finalisierung
        warn, log = Mock(), []
        d = self._diagnosis(rec.get_capture_seconds(), file_seconds=60.0,
                            video=progress_output(60_000_000),
                            audio=progress_output(60_000_000), warn=warn, log=log)
        d.run()
        warn.assert_not_called()
        self.assertFalse(any("BEFUND" in line for line in log))

    def test_really_short_file_reported_in_one_dialog(self):
        warn, log = Mock(), []
        d = self._diagnosis(60.0, file_seconds=30.0,
                            video=progress_output(30_000_000),
                            audio=progress_output(30_000_000), warn=warn, log=log)
        d.run()
        warn.assert_called_once()
        kind, capture, duration, warnings, truncated = warn.call_args[0]
        self.assertEqual(kind, "track_short")
        self.assertEqual((capture, duration), (60.0, 30.0))
        self.assertEqual(len(warnings), 2)
        self.assertTrue(truncated)
        self.assertTrue(any(line.startswith("BEFUND: Die Tonspur") for line in log))

    def test_container_truncation_only(self):
        warn = Mock()
        d = self._diagnosis(60.0, file_seconds=30.0,
                            video=progress_output(0), audio=progress_output(0),
                            warn=warn, log=[].append)
        d.run()
        warn.assert_called_once_with("truncation", 60.0, 30.0)

    def _diagnosis(self, capture, *, file_seconds, video, audio, warn, log):
        self.addCleanup(patch.stopall)
        patch("diagnosis.probe_duration_seconds", return_value=file_seconds).start()
        patch("diagnosis.probe_media_info",
              return_value="Stream #0:0: Video: h264, 30 fps\nStream #0:1: Audio: aac").start()
        patch("diagnosis.probe_max_volume_db", return_value=-20.0).start()
        patch("recording_validation.subprocess.Popen",
              PopenFactory(stdout=track_output(video, audio))).start()
        sink = log.append if isinstance(log, list) else log
        return diagnosis.DiagnosisThread("x.mp4", capture_seconds=capture, requested_fps=30,
                                         had_audio=True, audio_only=False,
                                         log=sink, warn=warn, ffmpeg="ffmpeg")


class WarningsDoNotFailRecording(unittest.TestCase):
    def test_success_stays_true(self):
        rec = recorder.RecorderThread({"output_path": "x.mp4", "audio_device": "mic"})
        rec._process = Mock(returncode=0)
        with patch("recorder.validate_recording", return_value=""), \
                patch("recorder.get_ffmpeg_path", return_value="ffmpeg"):
            self.assertTrue(rec._verify_output())
        # Die Spur-Endeprüfung liefert nur Warnungen, keinen Erfolgsstatus.
        with patch("recording_validation.subprocess.Popen",
                   PopenFactory(stdout=track_output(FIXTURE_VIDEO_20S, FIXTURE_AUDIO_5S))):
            warnings = rv.check_track_ends("x.mp4", "ffmpeg", expect_video=True,
                                           expect_audio=True, capture_seconds=20.0)
        self.assertIsInstance(warnings, list)
        self.assertTrue(warnings)


class SourceKind(unittest.TestCase):
    def test_three_cases(self):
        audio_map = {"🎤 Mikrofon (USB)": "mic-id", "🎤 Stereo Mix (Realtek)": "mix-id"}
        self.assertEqual(gui_logic.recording_source_kind("🎤 Stereo Mix (Realtek)", audio_map),
                         "systemton")
        self.assertEqual(gui_logic.recording_source_kind("🔊 Monitor of Built-in", {}), "systemton")
        self.assertEqual(gui_logic.recording_source_kind("🎤 Mikrofon (USB)", audio_map), "mikrofon")
        self.assertEqual(gui_logic.recording_source_kind("", audio_map), "unbekannt")
        self.assertEqual(gui_logic.recording_source_kind("Kein Audio", audio_map), "unbekannt")
        self.assertEqual(gui_logic.recording_source_kind("🎤 Weg", audio_map), "unbekannt")


# ============================================================================
# 4c Stille-Hinweis
# ============================================================================
class SilentAudioHint(unittest.TestCase):
    def test_windows(self):
        mic = diagnosis.silent_audio_hint("mikrofon", True)
        system = diagnosis.silent_audio_hint("systemton", True)
        unknown = diagnosis.silent_audio_hint("unbekannt", True)
        self.assertIn("Datenschutz", mic)
        self.assertIn("nichts abgespielt", system)
        for text in (mic, system, unknown):
            self.assertNotIn("Stereomix", text)
        self.assertNotIn("Datenschutz", unknown)
        self.assertNotIn("nichts abgespielt", unknown)

    def test_linux(self):
        texts = {kind: diagnosis.silent_audio_hint(kind, False)
                 for kind in ("mikrofon", "systemton", "unbekannt")}
        for text in texts.values():
            self.assertIn("pavucontrol", text)
            self.assertNotIn("Datenschutz", text)
        self.assertIn("nichts abgespielt", texts["systemton"])
        self.assertNotIn("nichts abgespielt", texts["unbekannt"])


# ============================================================================
# Regressionstest mit echtem ffmpeg (lavfi)
# ============================================================================
@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg erforderlich")
class LavfiRegression(unittest.TestCase):
    def test_video_20s_audio_5s(self):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "kurzer_ton.mp4")
            p = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10:duration=20",
                 "-f", "lavfi", "-i", "sine=duration=5",
                 "-c:v", "mpeg4", "-c:a", "aac", path],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            log = []
            warnings = rv.check_track_ends(path, ffmpeg, expect_video=True, expect_audio=True,
                                           capture_seconds=20.0, log=log.append)
        self.assertEqual(len(warnings), 1, log)
        self.assertIn("Tonspur", warnings[0])
        self.assertFalse(any("nicht ermittelbar" in line for line in log), log)


if __name__ == "__main__":
    unittest.main()
