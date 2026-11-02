"""Schließen und Abbruch der Worker (Recorder, Optimierer, Leistungstest, Diagnose) mit Fake-Prozessen."""
import sys; from pathlib import Path  # noqa: E702
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

import contextlib  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
from unittest.mock import Mock, patch  # noqa: E402

import benchmark  # noqa: E402
import diagnosis  # noqa: E402
import gui_logic  # noqa: E402
import optimizer  # noqa: E402
import recorder  # noqa: E402
from fake_processes import FakeClock, FakePsProcess, PopenFactory  # noqa: E402


def make_recorder(**callbacks):
    settings = {"output_path": "unbenutzt.mp4", "fps": "30", "encoder": "libx264",
                "preset": "veryfast", "audio_device": None, "audio_only": False}
    return recorder.RecorderThread(settings, **callbacks)


class RecorderPatches(unittest.TestCase):
    """Gemeinsame Patches: kein echtes FFmpeg, kein psutil, keine Wartezeit."""

    def setUp(self):
        self.factory = PopenFactory()
        self.ps = FakePsProcess()
        self.build = Mock(return_value=["ffmpeg", "-i", "x", "out.mp4"])
        for target, value in (
            ("recorder.subprocess.Popen", self.factory),
            ("recorder.psutil.Process", Mock(return_value=self.ps)),
            ("recorder.build_record_command", self.build),
            ("recorder.validate_recording", Mock(return_value="")),
            ("recorder.get_ffmpeg_path", Mock(return_value="ffmpeg")),
            ("recorder.START_CHECK_SECONDS", 0),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class RecorderShutdown(RecorderPatches):
    def test_stop_before_popen_starts_nothing(self):
        on_stopped = Mock()
        rec = make_recorder(on_stopped=on_stopped)
        rec.stop()
        rec.run()
        self.assertEqual(self.factory.instances, [])
        on_stopped.assert_called_once_with("unbenutzt.mp4", False)
        self.assertEqual(rec.get_capture_seconds(), 0.0)

    def test_stop_during_startphase_starts_nothing(self):
        # Abbruch während build_record_command (z. B. ddagrab-Prüfung)
        rec = make_recorder()
        self.build.side_effect = lambda **kw: (rec.stop(), ["ffmpeg", "out.mp4"])[1]
        rec.run()
        self.assertEqual(self.factory.instances, [])
        self.assertFalse(rec.success)

    def test_stop_between_check_and_publish_finalizes_with_q(self):
        on_started = Mock()
        on_stopped = Mock()
        rec = make_recorder(on_started=on_started, on_stopped=on_stopped)
        stopper = {}

        def on_create(proc):
            # stop() aus einem anderen Thread, während run() noch den Lock hält
            t = threading.Thread(target=rec.stop)
            t.start()
            stopper["t"] = t

        def ps_factory(pid):
            stopper["t"].join(timeout=5)   # deterministisch: stop() ist durch
            return self.ps

        self.factory.on_create = on_create
        with patch("recorder.psutil.Process", side_effect=ps_factory):
            rec.run()
        self.assertEqual(len(self.factory.instances), 1)
        proc = self.factory.instances[0]
        self.assertIn(b"q", proc.stdin.data)
        self.assertFalse(proc.killed)
        on_started.assert_not_called()
        on_stopped.assert_called_once_with("unbenutzt.mp4", True)

    def test_normal_stop_sends_q(self):
        rec = make_recorder()
        rec.on_started = rec.stop          # sofort nach dem Start stoppen
        rec.run()
        proc = self.factory.instances[0]
        self.assertEqual(proc.stdin.data, b"q")
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(rec.success)

    def test_force_kill_reads_reference_and_kills(self):
        rec = make_recorder()
        proc = self.factory(["ffmpeg"])
        rec._process = proc
        rec.force_kill()
        self.assertTrue(proc.killed)
        self.assertFalse(rec.is_active)

    def test_stop_is_idempotent_in_every_state(self):
        rec = make_recorder()
        rec.stop()
        rec.stop()
        self.assertTrue(rec._stop_event.is_set())
        self.assertFalse(rec.is_active)


class RecorderNoHardLimit(unittest.TestCase):
    """Kein Kill-Limit unter FINALIZE_MAX_SECONDS, solange FFmpeg arbeitet."""

    def _recorder(self, finish_at=None):
        clock = FakeClock(0.0)

        def on_wait(proc, timeout):
            clock.advance(timeout or 0)
            if finish_at is not None and clock.now >= finish_at:
                proc.returncode = 0

        factory = PopenFactory(on_wait=on_wait, finish_on_wait=False)
        rec = make_recorder()
        rec._clock = clock
        rec._process = factory(["ffmpeg"])
        rec._ps_process = FakePsProcess(active=True)
        return rec, clock

    def test_busy_ffmpeg_is_waited_for_until_max(self):
        rec, clock = self._recorder()
        self.assertFalse(rec._wait_while_finalizing())
        self.assertGreaterEqual(clock.now, recorder.FINALIZE_MAX_SECONDS)
        self.assertFalse(rec._process.killed)

    def test_long_finalization_below_max_succeeds(self):
        rec, clock = self._recorder(finish_at=400)
        self.assertTrue(rec._wait_while_finalizing())
        self.assertGreaterEqual(clock.now, 400)
        self.assertFalse(rec._process.killed)

    def test_gui_safety_margin_above_max(self):
        self.assertEqual(recorder.FINALIZE_MAX_SECONDS, 900)
        self.assertEqual(recorder.FINALIZE_IDLE_SECONDS, 15)
        self.assertEqual(recorder.FINALIZE_BLIND_SECONDS, 60)
        # Das Wartebudget der übrigen Worker gilt NICHT für den Recorder.
        self.assertLess(gui_logic.WORKER_SHUTDOWN_BUDGET_SECONDS, recorder.FINALIZE_MAX_SECONDS)


class RecorderPause(unittest.TestCase):
    def _running(self):
        rec = make_recorder()
        rec._process = PopenFactory()(["ffmpeg"])
        rec._ps_process = FakePsProcess()
        rec._clock = FakeClock()
        rec._start_time = rec._clock()
        return rec

    def test_toggle_pause_after_stop_does_not_suspend(self):
        rec = self._running()
        rec.stop()
        self.assertFalse(rec.toggle_pause())
        self.assertEqual(rec._ps_process.suspend_calls, 0)

    def test_toggle_pause_after_freeze_returns_old_state(self):
        rec = self._running()
        self.assertTrue(rec.toggle_pause())        # pausiert
        rec._freeze_capture_seconds()
        self.assertTrue(rec.toggle_pause())        # gesperrt: alter Zustand
        self.assertEqual(rec._ps_process.resume_calls, 0)

    def test_stop_while_paused_resumes_exactly_once(self):
        rec = self._running()
        rec._process.factory.exit_on_q = True
        self.assertTrue(rec.toggle_pause())
        rec.stop()
        stop_flag = threading.Event()

        def hammer():
            while not stop_flag.is_set():
                rec.toggle_pause()

        t = threading.Thread(target=hammer)
        t.start()
        try:
            with patch("recorder.validate_recording", return_value=""), \
                    patch("recorder.get_ffmpeg_path", return_value="ffmpeg"):
                self.assertTrue(rec._graceful_stop())
        finally:
            stop_flag.set()
            t.join(timeout=5)
        self.assertEqual(rec._ps_process.resume_calls, 1)
        self.assertEqual(rec._ps_process.suspend_calls, 1)
        self.assertFalse(rec.is_paused)


class OptimizerShutdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.input = os.path.join(self.tmp.name, "a.mp4")
        self.output = os.path.join(self.tmp.name, "a_optimiert.mp4")
        with open(self.input, "wb") as f:
            f.write(b"x" * 4096)
        with open(self.output, "wb") as f:
            f.write(b"unfertig")
        patcher = patch("optimizer.get_ffmpeg_path", return_value="ffmpeg")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _thread(self):
        self.on_error = Mock()
        self.on_finish = Mock()
        return optimizer.OptimizeThread(self.input, self.output,
                                        optimizer.get_profile("balanced"),
                                        on_error=self.on_error, on_finish=self.on_finish)

    def test_cancel_before_popen_starts_nothing_and_no_on_error(self):
        factory = PopenFactory()
        t = self._thread()
        t.cancel()
        with patch("optimizer.subprocess.Popen", factory):
            t.run()
        self.assertEqual(factory.instances, [])
        self.on_error.assert_not_called()
        self.assertFalse(os.path.exists(self.output))

    def test_cancel_during_duration_probe_kills_it(self):
        t = self._thread()
        factory = PopenFactory(on_communicate=lambda proc: t.cancel())
        with patch("optimizer.subprocess.Popen", factory):
            t.run()
        self.assertEqual(len(factory.instances), 1)        # nur die Sondierung
        self.assertTrue(factory.instances[0].killed)
        self.on_error.assert_not_called()
        self.on_finish.assert_not_called()
        self.assertFalse(os.path.exists(self.output))

    def test_failed_encode_removes_partial_output(self):
        t = self._thread()
        factory = PopenFactory(returncode_on_finish=1)
        with patch("optimizer.subprocess.Popen", factory):
            t.run()
        self.assertFalse(os.path.exists(self.output))
        self.on_error.assert_called_once()
        self.assertTrue(os.path.exists(self.input))       # Original bleibt

    def test_cancel_during_encode_removes_partial_output(self):
        t = self._thread()
        created = []

        def on_create(proc):
            created.append(proc)
            if len(created) == 2:          # Kodierlauf: Abbruch nach dem Start
                threading.Thread(target=t.cancel).start()

        def on_wait(proc, timeout):
            # Kodierlauf endet erst durch cancel()
            for _ in range(500):
                if proc.killed:
                    break
                threading.Event().wait(0.01)

        factory = PopenFactory(on_create=on_create, on_wait=on_wait)
        with patch("optimizer.subprocess.Popen", factory):
            t.run()
        self.assertEqual(len(created), 2)
        self.assertTrue(created[1].killed)
        self.on_error.assert_not_called()
        self.assertFalse(os.path.exists(self.output))


class BenchmarkShutdown(unittest.TestCase):
    def test_cancel_before_stage_starts_nothing(self):
        factory = PopenFactory()
        bench = benchmark.BenchmarkThread()
        bench.cancel()
        with patch("benchmark.subprocess.Popen", factory):
            with self.assertRaises(InterruptedError):
                bench._run_stage(["ffmpeg"], seconds=1, sample_cpu=False, label="x")
        self.assertEqual(factory.instances, [])

    def test_cancel_racing_popen_kills_new_process(self):
        bench = benchmark.BenchmarkThread()
        threads = []

        def on_create(proc):
            t = threading.Thread(target=bench.cancel)   # wartet auf den Lock
            t.start()
            threads.append(t)

        factory = PopenFactory(on_create=on_create)
        with patch("benchmark.subprocess.Popen", factory):
            with self.assertRaises(InterruptedError):
                bench._run_stage(["ffmpeg"], seconds=5, sample_cpu=False, label="x")
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(factory.instances), 1)
        self.assertTrue(factory.instances[0].killed)

    def test_cancelled_run_reports_no_error(self):
        on_error = Mock()
        on_finish = Mock()
        bench = benchmark.BenchmarkThread(on_error=on_error, on_finish=on_finish)
        bench.cancel()
        with patch("benchmark.subprocess.Popen", PopenFactory()), \
                patch("benchmark.build_benchmark_command", return_value=["ffmpeg"]):
            bench.run()
        on_error.assert_not_called()
        on_finish.assert_not_called()


class DiagnosisShutdown(unittest.TestCase):
    def test_cancel_during_probe_kills_it_and_stops(self):
        warn = Mock()
        log = []
        d = diagnosis.DiagnosisThread("datei.mp4", capture_seconds=30, had_audio=True,
                                      log=log.append, warn=warn, ffmpeg="ffmpeg")
        factory = PopenFactory(on_communicate=lambda proc: d.cancel())
        with patch("optimizer.subprocess.Popen", factory), \
                patch("optimizer.get_ffmpeg_path", return_value="ffmpeg"), \
                patch("recording_validation.subprocess.Popen", factory):
            d.run()
        self.assertEqual(len(factory.instances), 1)
        self.assertTrue(factory.instances[0].killed)
        warn.assert_not_called()
        self.assertTrue(d.cancelled)

    def test_cancel_before_start_runs_nothing(self):
        d = diagnosis.DiagnosisThread("datei.mp4", capture_seconds=30, ffmpeg="ffmpeg")
        d.cancel()
        factory = PopenFactory()
        with patch("optimizer.subprocess.Popen", factory), \
                patch("recording_validation.subprocess.Popen", factory):
            d.run()
        self.assertEqual(factory.instances, [])


class _HangingWorker:
    """Worker, der nie von selbst endet; join() lässt die Fake-Uhr laufen."""

    def __init__(self, clock, dies=False):
        self.clock = clock
        self.cancel_calls = 0
        self.kill_calls = 0
        self.dies = dies

    def cancel(self):
        self.cancel_calls += 1

    def kill(self):
        self.kill_calls += 1

    def is_alive(self):
        return not (self.dies and self.cancel_calls)

    def join(self, timeout=None):
        self.clock.advance(timeout or 0)


class WorkerBudget(unittest.TestCase):
    def test_budget_10s_then_hard_kill_with_stderr_prefix(self):
        clock = FakeClock(0.0)
        a, b = _HangingWorker(clock), _HangingWorker(clock)
        pumps = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            left = gui_logic.wait_for_workers([a, b, None], pump=lambda: pumps.append(1),
                                              clock=clock)
        self.assertEqual(left, [a, b])
        self.assertEqual((a.cancel_calls, a.kill_calls), (1, 1))
        self.assertEqual((b.cancel_calls, b.kill_calls), (1, 1))
        self.assertGreaterEqual(clock.now, 10.0)
        self.assertLess(clock.now, 10.5)                 # gemeinsames Budget
        self.assertTrue(pumps)
        lines = [ln for ln in err.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("[ScreenRecPro] "), line)

    def test_finished_workers_no_wait_no_stderr(self):
        clock = FakeClock(0.0)
        w = _HangingWorker(clock, dies=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(gui_logic.wait_for_workers([w], clock=clock), [])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(w.kill_calls, 0)
        self.assertEqual(clock.now, 0.0)


class InStartupProperty(unittest.TestCase):
    """SRP1-IR-02: in_startup ist nur in der Startphase wahr (Thread lebt, kein Prozess)."""

    def test_in_startup_only_without_process_and_alive(self):
        import recorder
        rec = recorder.RecorderThread({"output_path": "x.mp4"})
        self.assertFalse(rec.in_startup)              # Thread noch nicht gestartet
        with patch.object(rec, "is_alive", return_value=True):
            self.assertTrue(rec.in_startup)           # lebt, kein Prozess
            rec._process = Mock()
            self.assertFalse(rec.in_startup)          # Prozess veröffentlicht


if __name__ == "__main__":
    unittest.main()
