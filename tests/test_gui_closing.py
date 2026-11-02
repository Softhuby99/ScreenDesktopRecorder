"""
Schließdialog, Zustand "schließt" und Sperre in _post_to_gui - ohne Display.

GUI-freie Entscheidungslogik (gui_logic) wird immer getestet. MainWindow-
Methoden laufen als ungebundene Aufrufe mit einer Attrappe als self
(types.SimpleNamespace) und gepatchtem tkinter.messagebox; ohne
customtkinter wird dieser Teil übersprungen (Skip statt Fehler).
"""
import sys; from pathlib import Path  # noqa: E702
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import py_compile  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from unittest.mock import Mock, patch  # noqa: E402

import gui_logic  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parents[1]
HAS_GUI = (importlib.util.find_spec("customtkinter") is not None
           and importlib.util.find_spec("tkinter") is not None)


# ============================================================================
# GUI-freie Logik (läuft immer)
# ============================================================================
class GuiSourcesCompile(unittest.TestCase):
    def test_gui_modules_compile(self):
        # Syntaxprüfung auch dort, wo customtkinter fehlt
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("gui_main.py", "gui_mini.py"):
                py_compile.compile(str(PROJECT_DIR / name),
                                   cfile=os.path.join(tmp, name + "c"), doraise=True)


class CloseLogic(unittest.TestCase):
    def test_recording_running_uses_is_alive_not_is_active(self):
        startphase = types.SimpleNamespace(is_alive=lambda: True, is_active=False)
        finished = types.SimpleNamespace(is_alive=lambda: False, is_active=True)
        self.assertTrue(gui_logic.recording_running(startphase))
        self.assertFalse(gui_logic.recording_running(finished))
        self.assertFalse(gui_logic.recording_running(None))

    def test_close_choice(self):
        self.assertEqual(gui_logic.close_choice(True), gui_logic.CLOSE_SAVE)
        self.assertEqual(gui_logic.close_choice(False), gui_logic.CLOSE_DISCARD)
        self.assertEqual(gui_logic.close_choice(None), gui_logic.CLOSE_CANCEL)

    def test_post_allowed(self):
        def _set_status():
            pass

        def _warn_silent_audio():
            pass

        self.assertTrue(gui_logic.post_allowed(False, _warn_silent_audio))
        self.assertFalse(gui_logic.post_allowed(True, _warn_silent_audio))
        self.assertTrue(gui_logic.post_allowed(True, _set_status))
        self.assertFalse(gui_logic.post_allowed(True, lambda: None))

    def test_remove_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            Path(path).write_bytes(b"x")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertTrue(gui_logic.remove_partial_output(path))
                self.assertFalse(os.path.exists(path))
                # fehlende Datei: kein Fehler, keine Meldung
                self.assertTrue(gui_logic.remove_partial_output(path))
            self.assertEqual(err.getvalue(), "")

    def test_wait_for_recorder_startup_returns_at_once_without_startphase(self):
        rec = Mock(in_startup=False)
        self.assertTrue(gui_logic.wait_for_recorder_startup(rec))
        rec.join.assert_not_called()

    def test_wait_for_recorder_startup_waits_until_thread_ends(self):
        rec = Mock(in_startup=True)
        rec.is_alive.side_effect = [True, True, False]
        pump = Mock()
        self.assertTrue(gui_logic.wait_for_recorder_startup(rec, pump=pump))
        self.assertEqual(rec.join.call_count, 2)
        self.assertEqual(pump.call_count, 2)

    def test_wait_for_recorder_startup_gives_up_after_budget(self):
        rec = Mock(in_startup=True)
        rec.is_alive.return_value = True
        ticks = iter([0.0, 1.0, 20.0])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(gui_logic.wait_for_recorder_startup(
                rec, budget_seconds=12.0, clock=lambda: next(ticks)))
        self.assertTrue(err.getvalue().startswith("[ScreenRecPro] "))

    def test_remove_partial_output_other_oserror_logged(self):
        err = io.StringIO()
        with patch("gui_logic.os.remove", side_effect=PermissionError("gesperrt")), \
                contextlib.redirect_stderr(err):
            self.assertFalse(gui_logic.remove_partial_output("x.mp4"))
        self.assertTrue(err.getvalue().startswith("[ScreenRecPro] "))


# ============================================================================
# MainWindow mit Attrappen-self
# ============================================================================
if HAS_GUI:
    import gui_main as gm  # noqa: E402
    import gui_mini  # noqa: E402
else:  # pragma: no cover
    gm = None
    gui_mini = None


class FakeRecorder:
    def __init__(self, path, alive=True, active=False, order=None):
        self.output_path = path
        self._alive = alive
        self.is_active = active
        self.order = order if order is not None else []
        self.is_paused = False
        self.success = True
        self.toggle_pause = Mock(return_value=True)

    def is_alive(self):
        return self._alive

    def stop(self):
        self.order.append("stop")

    def force_kill(self):
        self.order.append("force_kill")

    def join(self, timeout=None):
        pass


def bind(ns, *names):
    for name in names:
        setattr(ns, name, types.MethodType(getattr(gm.MainWindow, name), ns))


def make_window(**overrides):
    ns = types.SimpleNamespace(
        _closing=False, _close_dialog_open=False, recorder=None, mini_panel=None,
        _timer_job=None, _meter_poll_job=None, _region_job=None,
        benchmark_thread=None, _optimize_thread=None, _diagnosis_thread=None,
        _recording_source_kind="unbekannt", _active_audio_only=False,
        _close_overlay=None,
    )
    ns.destroy = Mock()
    ns.after = Mock()
    ns.after_cancel = Mock()
    ns.update = Mock()
    ns.withdraw = Mock()
    ns.deiconify = Mock()
    ns.lift = Mock()
    ns._show_close_overlay = Mock()
    ns._finish_close = Mock()
    ns._wait_for_recorder_shutdown = Mock()
    ns._write_closing_log = Mock()
    ns._pump_events = Mock()
    ns.status = []

    def _set_status(text, color=None):
        ns.status.append(text)

    ns._set_status = _set_status
    bind(ns, "_on_close", "_begin_closing", "_discard_recording", "_post_to_gui", "_run_posted")
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


@unittest.skipUnless(HAS_GUI, "customtkinter nicht installiert")
class CloseDialog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "Aufnahme.mp4")
        Path(self.path).write_bytes(b"x" * 2048)
        self.order = []
        self.rec = FakeRecorder(self.path, order=self.order)
        self.win = make_window(recorder=self.rec)
        patcher = patch.object(gm, "messagebox")
        self.mb = patcher.start()
        self.addCleanup(patcher.stop)

    def test_yes_stops_finalizes_and_keeps_file(self):
        self.mb.askyesnocancel.return_value = True
        self.win._on_close()
        self.assertEqual(self.order, ["stop"])
        self.win._wait_for_recorder_shutdown.assert_called_once_with(self.rec)
        self.win._write_closing_log.assert_called_once_with(self.rec)
        self.win._finish_close.assert_called_once_with(wait_recorder=False)
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(self.win._closing)
        self.win._show_close_overlay.assert_called_once()

    def test_no_kills_and_deletes_in_order(self):
        self.mb.askyesnocancel.return_value = False
        real_remove = gui_logic.remove_partial_output

        def remove(path):
            self.order.append("remove")
            return real_remove(path)

        err = io.StringIO()
        with patch.object(gm, "remove_partial_output", side_effect=remove), \
                contextlib.redirect_stderr(err):
            self.win._on_close()
        self.assertEqual(self.order, ["stop", "force_kill", "remove"])
        self.assertFalse(os.path.exists(self.path))
        self.win._wait_for_recorder_shutdown.assert_not_called()
        self.win._write_closing_log.assert_not_called()
        self.win._finish_close.assert_called_once_with(wait_recorder=False)
        self.assertEqual(self.mb.askyesnocancel.call_count, 1)
        self.assertEqual(self.mb.showwarning.call_count + self.mb.showerror.call_count
                         + self.mb.showinfo.call_count, 0)
        self.assertTrue(err.getvalue().startswith("[ScreenRecPro] "))

    def test_no_in_startphase_waits_for_precheck_before_delete(self):
        self.rec.in_startup = True
        real_remove = gui_logic.remove_partial_output

        def wait(rec, pump=None):
            self.order.append("wait")
            return True

        def remove(path):
            self.order.append("remove")
            return real_remove(path)

        self.mb.askyesnocancel.return_value = False
        with patch.object(gm, "wait_for_recorder_startup", side_effect=wait), \
                patch.object(gm, "remove_partial_output", side_effect=remove), \
                contextlib.redirect_stderr(io.StringIO()):
            self.win._on_close()
        self.assertEqual(self.order, ["stop", "force_kill", "wait", "remove"])
        self.win._finish_close.assert_called_once_with(wait_recorder=False)

    def test_no_with_missing_file_still_closes(self):
        os.remove(self.path)
        self.mb.askyesnocancel.return_value = False
        with contextlib.redirect_stderr(io.StringIO()):
            self.win._on_close()
        self.win._finish_close.assert_called_once_with(wait_recorder=False)

    def test_no_with_failing_delete_still_destroys(self):
        self.mb.askyesnocancel.return_value = False
        with patch.object(gm, "remove_partial_output", side_effect=OSError("x")):
            with self.assertRaises(OSError):
                self.win._on_close()
        self.win._finish_close.assert_called_once_with(wait_recorder=False)

    def test_cancel_changes_nothing_and_asks_again(self):
        self.mb.askyesnocancel.return_value = None
        self.win._on_close()
        self.assertEqual(self.order, [])
        self.assertFalse(self.win._closing)
        self.assertFalse(self.win._close_dialog_open)
        self.win._finish_close.assert_not_called()
        self.win._show_close_overlay.assert_not_called()
        self.win.after_cancel.assert_not_called()
        self.win.destroy.assert_not_called()
        self.assertTrue(os.path.exists(self.path))
        self.win._on_close()
        self.assertEqual(self.mb.askyesnocancel.call_count, 2)

    def test_second_close_while_dialog_open_is_noop(self):
        def answer(*args, **kwargs):
            self.assertTrue(self.win._close_dialog_open)
            self.win._on_close()             # zweiter Schließversuch
            return None

        self.mb.askyesnocancel.side_effect = answer
        self.win._on_close()
        self.assertEqual(self.mb.askyesnocancel.call_count, 1)
        self.assertFalse(self.win._close_dialog_open)
        self.assertFalse(self.win._closing)

    def test_dialog_flag_reset_even_on_exception(self):
        self.mb.askyesnocancel.side_effect = RuntimeError("Tk weg")
        with self.assertRaises(RuntimeError):
            self.win._on_close()
        self.assertFalse(self.win._close_dialog_open)

    def test_startphase_gets_dialog_not_destroy(self):
        rec = FakeRecorder(self.path, alive=True, active=False)
        win = make_window(recorder=rec)
        self.mb.askyesnocancel.return_value = None
        win._on_close()
        self.mb.askyesnocancel.assert_called_once()
        win._finish_close.assert_not_called()
        win.destroy.assert_not_called()

    def test_without_recording_closes_without_question(self):
        win = make_window(recorder=None)
        win._on_close()
        self.mb.askyesnocancel.assert_not_called()
        self.assertTrue(win._closing)
        win._finish_close.assert_called_once_with(wait_recorder=True)

    def test_close_while_closing_is_noop(self):
        self.win._closing = True
        self.win._on_close()
        self.mb.askyesnocancel.assert_not_called()
        self.win._finish_close.assert_not_called()


@unittest.skipUnless(HAS_GUI, "customtkinter nicht installiert")
class FinishClose(unittest.TestCase):
    def test_waits_for_live_recorder_and_workers_then_destroys(self):
        rec = FakeRecorder("x.mp4")
        win = make_window(recorder=rec)
        bind(win, "_finish_close")
        win._save_settings = Mock()
        win._pause_meters = Mock()
        win._pump_events = Mock()
        worker = Mock()
        win._optimize_thread = worker
        with patch.object(gm, "wait_for_workers") as wfw:
            win._finish_close(wait_recorder=True)
        self.assertEqual(rec.order, ["stop"])
        win._wait_for_recorder_shutdown.assert_called_once_with(rec)
        wfw.assert_called_once()
        self.assertIn(worker, wfw.call_args[0][0])
        win.destroy.assert_called_once()

    def test_destroy_even_if_waiting_fails(self):
        win = make_window()
        bind(win, "_finish_close")
        win._save_settings = Mock()
        win._pause_meters = Mock()
        win._pump_events = Mock()
        with patch.object(gm, "wait_for_workers", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                win._finish_close()
        win.destroy.assert_called_once()

    def test_wait_for_recorder_shutdown_uses_local_reference(self):
        rec = FakeRecorder("x.mp4")
        states = [True, False]
        rec.is_alive = lambda: states.pop(0) if len(states) > 1 else states[0]
        win = make_window(recorder=rec)
        rec.force_kill = Mock()

        def update():
            win.recorder = None               # Callback setzt self.recorder zurück

        win.update = update
        gm.MainWindow._wait_for_recorder_shutdown(win, rec)
        rec.force_kill.assert_not_called()


@unittest.skipUnless(HAS_GUI, "customtkinter nicht installiert")
class ClosingState(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(gm, "messagebox")
        self.mb = patcher.start()
        self.addCleanup(patcher.stop)
        self.win = make_window(_closing=True)
        # after() führt sofort aus - so würde ein durchgelassener Auftrag laufen
        self.win.after = lambda ms, fn, *args: fn(*args)

    def mb_calls(self):
        return len(self.mb.method_calls)

    def test_begin_closing_cancels_jobs_and_locks_minipanel(self):
        win = make_window(_timer_job="t", _meter_poll_job="m", _region_job="r")
        win.mini_panel = Mock()
        win._begin_closing("x")
        self.assertTrue(win._closing)
        self.assertEqual(sorted(c.args[0] for c in win.after_cancel.call_args_list),
                         ["m", "r", "t"])
        win.mini_panel.unbind.assert_called_once_with("<space>")
        self.assertIsNone(win.mini_panel.on_pause)
        self.assertIsNone(win._timer_job)
        win._show_close_overlay.assert_called_once_with("x")

    def test_no_warning_path_opens_dialog(self):
        names = ["_warn_unreadable_file", "_warn_no_audio_stream", "_warn_silent_audio",
                 "_warn_possible_truncation", "_warn_track_too_short", "_apply_encoder_check",
                 "_apply_audio_devices", "_apply_meter_devices", "_on_recording_stopped",
                 "_on_recording_error", "_on_recording_started", "_bench_finish",
                 "_bench_error", "_on_optimize_finish", "_on_optimize_error"]
        bind(self.win, *names)
        for name in names:
            self.win._post_to_gui(getattr(self.win, name), "a", "b", "c", "d", "e")
        self.assertEqual(self.mb_calls(), 0)
        self.assertEqual(self.win.status, [])

    def test_diagnosis_warnings_dropped_while_closing(self):
        bind(self.win, "_on_diagnosis_warning", "_warn_track_too_short", "_warn_silent_audio",
             "_warn_unreadable_file", "_warn_no_audio_stream", "_warn_possible_truncation")
        for kind, details in (("unreadable", ()), ("no_audio", ()), ("silent", ("mikrofon",)),
                              ("truncation", (60.0, 30.0)),
                              ("track_short", (60.0, 30.0, ["w"], True))):
            self.win._on_diagnosis_warning(kind, None, *details)
        self.assertEqual(self.mb_calls(), 0)

    def test_allowed_jobs_still_run(self):
        self.win._post_to_gui(self.win._set_status, "Protokoll")
        self.assertEqual(self.win.status, ["Protokoll"])

    def test_queued_job_rechecked_at_runtime(self):
        win = make_window()
        queued = []
        win.after = lambda ms, fn, *args: queued.append((fn, args))
        bind(win, "_warn_unreadable_file")
        win._post_to_gui(win._warn_unreadable_file, None)
        win._closing = True                    # Schließen beginnt
        for fn, args in queued:
            fn(*args)
        self.assertEqual(self.mb_calls(), 0)

    def test_destroyed_window_errors_are_swallowed(self):
        win = make_window()
        win.after = Mock(side_effect=RuntimeError("application has been destroyed"))
        bind(win, "_warn_unreadable_file")
        win._post_to_gui(win._warn_unreadable_file, None)    # kein Fehler

    def test_encoder_change_starts_no_thread(self):
        self.win._check_encoder_async = Mock()
        bind(self.win, "_on_encoder_change")
        with patch.object(gm.threading, "Thread") as thread:
            self.win._on_encoder_change("libx265")
        thread.assert_not_called()
        self.win._check_encoder_async.assert_not_called()

    def test_toggle_pause_changes_nothing(self):
        rec = FakeRecorder("x.mp4")
        rec.is_paused = True
        self.win.recorder = rec
        self.assertTrue(gm.MainWindow._toggle_pause(self.win))
        rec.toggle_pause.assert_not_called()
        self.win.recorder = None
        self.assertFalse(gm.MainWindow._toggle_pause(self.win))

    def test_space_in_minipanel_changes_nothing(self):
        panel = types.SimpleNamespace(on_pause=None, pause_btn=Mock(), fps_label=Mock(),
                                      _paused=False)
        gui_mini.MiniPanel._handle_pause(panel)
        panel.pause_btn.configure.assert_not_called()
        panel.fps_label.configure.assert_not_called()
        self.assertFalse(panel._paused)

    def test_minipanel_stop_unbinds_space(self):
        panel = types.SimpleNamespace(on_pause=Mock(), on_stop=Mock(), unbind=Mock(),
                                      stop_btn=Mock(), pause_btn=Mock())
        gui_mini.MiniPanel._handle_stop(panel)
        panel.unbind.assert_called_once_with("<space>")
        self.assertIsNone(panel.on_pause)
        panel.on_stop.assert_called_once()

    def test_start_recording_creates_no_thread(self):
        with patch.object(gm, "RecorderThread") as rt:
            gm.MainWindow._start_recording(self.win)
        rt.assert_not_called()
        self.assertIsNone(self.win.recorder)

    def test_other_entry_points_are_noops(self):
        with patch.object(gm, "BenchmarkThread") as bt, \
                patch.object(gm, "OptimizeThread") as ot, \
                patch.object(gm, "DiagnosisThread") as dt:
            gm.MainWindow._start_benchmark(self.win)
            gm.MainWindow._start_optimize(self.win)
            gm.MainWindow._stop_recording(self.win)
            gm.MainWindow._on_mode_change(self.win, gm.MODE_REGION)
            gm.MainWindow._start_diagnosis(self.win, "x.mp4", 30.0, None)
        bt.assert_not_called()
        ot.assert_not_called()
        dt.assert_not_called()
        self.win.withdraw.assert_not_called()
        self.assertEqual(self.mb_calls(), 0)

    def test_recording_stopped_and_started_do_nothing(self):
        with patch.object(gm, "MiniPanel") as mp:
            gm.MainWindow._on_recording_started(self.win)
            gm.MainWindow._on_recording_stopped(self.win, "x.mp4", True)
        mp.assert_not_called()
        self.win.withdraw.assert_not_called()
        self.win.deiconify.assert_not_called()
        self.assertEqual(self.mb_calls(), 0)


@unittest.skipUnless(HAS_GUI, "customtkinter nicht installiert")
class WarningDialogs(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(gm, "messagebox")
        self.mb = patcher.start()
        self.addCleanup(patcher.stop)
        self.win = make_window()
        self.win.after = lambda ms, fn, *args: fn(*args)
        bind(self.win, "_on_diagnosis_warning", "_warn_track_too_short",
             "_warn_possible_truncation", "_warn_silent_audio")

    def test_short_track_and_truncation_one_dialog(self):
        self.win._on_diagnosis_warning("track_short", "x_aufnahme-log.txt",
                                       60.0, 30.0, ["Die Tonspur endet ..."], True)
        self.assertEqual(self.mb.showwarning.call_count, 1)
        text = self.mb.showwarning.call_args[0][1]
        self.assertIn("Tonspur", text)
        self.assertIn("30 s", text)

    def test_silent_hint_by_source_kind(self):
        with patch.object(gm, "IS_WINDOWS", True):
            self.win._on_diagnosis_warning("silent", None, "systemton")
        text = self.mb.showwarning.call_args[0][1]
        self.assertIn("nichts abgespielt", text)
        self.assertNotIn("Stereomix", text)

    def test_write_log_with_zero_capture(self):
        win = make_window()
        win._capture_method_from = gm.MainWindow._capture_method_from
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "Aufnahme.mp4")
            log_path = gm.MainWindow._write_recording_log(win, video, [], [], 0.0, False)
            self.assertIsNotNone(log_path)
            content = Path(log_path).read_text(encoding="utf-8")
        self.assertIn("Aufnahmedauer:  0.0s", content)
        self.assertIn("FEHLGESCHLAGEN", content)


if __name__ == "__main__":
    unittest.main()
