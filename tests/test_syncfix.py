import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import calibration as c
import syncfix as s
from ffmpeg_utils import build_record_command


class PureTest(unittest.TestCase):
    def test_choose_cut(self):
        keys = [0.0, 0.5, 1.0, 6.5, 7.0, 7.5]
        self.assertEqual(s.choose_cut(keys, 6.6), 7.0)
        self.assertEqual(s.choose_cut(keys, 6.0), 6.5)
        self.assertIsNone(s.choose_cut(keys, 9.0))

    def test_fix_command(self):
        with mock.patch.object(s, "get_ffmpeg_path", return_value="ffmpeg"):
            cmd = s.build_fix_command("in.mp4", "out.mp4", 7.0, 310)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "7.000000")
        second = cmd.index("-ss", cmd.index("-ss") + 1)
        self.assertEqual(cmd[second + 1], "7.310000")
        self.assertIn("copy", cmd)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_fix_command_audio_early(self):
        with mock.patch.object(s, "get_ffmpeg_path", return_value="ffmpeg"):
            cmd = s.build_fix_command("in.mp4", "out.mp4", 7.0, -200)
        second = cmd.index("-ss", cmd.index("-ss") + 1)
        self.assertEqual(cmd[second + 1], "6.800000")

    def test_pick_marker_flashes_by_raster(self):
        # 6.5: Bildinhalt wird hell, als das Marker-Fenster schließt
        self.assertEqual(s.pick_marker_flashes([2.0, 4.03, 6.0, 6.5, 9.0]), [2.0, 4.03, 6.0])
        # erster Blitz verpasst (FFmpeg lief noch nicht): zwei bleiben
        self.assertEqual(s.pick_marker_flashes([4.0, 6.0]), [4.0, 6.0])
        # Störung vor dem Marker
        self.assertEqual(s.pick_marker_flashes([0.4, 2.0, 4.0, 6.0]), [2.0, 4.0, 6.0])
        self.assertEqual(s.pick_marker_flashes([]), [])

    def test_earliest_cut_covers_clock_lead_and_last_flash(self):
        # App-Uhr 6,2 s; Dateizeit kann bis ca. 1 s voraus sein
        self.assertGreaterEqual(s.earliest_cut(6.2, None), 6.2 + 1.0)
        # letzter Blitz spät in der Datei -> Schnitt sicher hinter Signalende
        self.assertGreaterEqual(s.earliest_cut(6.2, 6.9), 6.9 + 0.42 + 1.0)

    def test_check_fixed_file(self):
        orig = {"duration": 60.0, "video": True, "audio": True}
        self.assertEqual(s.check_fixed_file(orig, {"duration": 52.3, "video": True, "audio": True}, 8.0), "")
        self.assertIn("Spuren", s.check_fixed_file(orig, {"duration": 52.0, "video": True, "audio": False}, 8.0))
        self.assertIn("unplausibel", s.check_fixed_file(orig, {"duration": 20.0, "video": True, "audio": True}, 8.0))
        self.assertIn("nicht lesbar", s.check_fixed_file(orig, {"duration": None, "video": True, "audio": True}, 8.0))

    def test_describe(self):
        ok = {"fixed": True, "lateness_ms": 310, "cut_s": 7.0, "spread_ms": 10, "pairs": 3}
        self.assertIn("310 ms zu spät", s.describe_fix(ok))
        self.assertIn("unverändert", s.describe_fix({"fixed": False, "reason": "x"}))

    def test_record_command_marker_keyframes(self):
        with mock.patch("ffmpeg_utils.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("ffmpeg_utils._should_use_ddagrab", return_value=False):
            with_m = build_record_command("o.mp4", "30", "libx264", "ultrafast",
                                          audio_device="Mic", screen_size=(640, 360),
                                          marker_keyframes=True)
            without = build_record_command("o.mp4", "30", "libx264", "ultrafast",
                                           audio_device="Mic", screen_size=(640, 360))
        self.assertIn("-force_key_frames", with_m)
        self.assertNotIn("-force_key_frames", without)
        self.assertEqual(with_m[-1], "o.mp4")


class FakePlayer:
    def __init__(self, on_beep, beeps=3, interval=2.0):
        self.on_beep, self.beeps = on_beep, beeps
        self.done = threading.Event()
        self.device_info, self.latency_s = "Fake", 0.02

    def start(self):
        for i in range(self.beeps):
            self.on_beep(float(i))
        self.done.set()

    def stop(self):
        self.done.set()


class MarkerThreadTest(unittest.TestCase):
    def test_plays_and_reports(self):
        flashes, done = [], []
        with mock.patch.object(s, "MARKER_LEAD_S", 0.01):
            t = s.MarkerThread(on_flash=flashes.append, on_done=done.append,
                               player_factory=FakePlayer)
            t.start()
            t.join(5)
        self.assertEqual(len(flashes), s.MARKER_BEEPS)
        self.assertTrue(done[0]["ok"])
        self.assertEqual(done[0]["latency_ms"], 20)
        self.assertEqual(done[0]["underruns"], 0)

    def test_cancel(self):
        done = []
        t = s.MarkerThread(on_done=done.append, player_factory=FakePlayer)
        t.cancel()
        t.start()
        t.join(5)
        self.assertFalse(done[0]["ok"])


class SyncFixThreadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "a.mp4")
        with open(self.path, "wb") as f:
            f.write(b"original")

    def test_failed_analysis_leaves_file(self):
        res = {}
        t = s.SyncFixThread(self.path, None, 6.0, on_finish=res.update,
                            analyze=lambda p, crop, end: {"ok": False, "reason": "kein Marker"})
        t.start()
        t.join(5)
        self.assertFalse(res["fixed"])
        self.assertEqual(open(self.path, "rb").read(), b"original")
        self.assertEqual(os.listdir(self.dir), ["a.mp4"])

    def test_failed_ffmpeg_leaves_file(self):
        class Proc:
            returncode = 1

            def __init__(self, *a, **k):
                pass

            def communicate(self):
                return b"", b"boom"

            def poll(self):
                return 1
        res = {}
        t = s.SyncFixThread(self.path, None, 6.0, on_finish=res.update,
                            analyze=lambda p, crop, end: {"ok": True, "cut_s": 7.0, "lateness_ms": 100},
                            popen=Proc)
        t.start()
        t.join(5)
        self.assertFalse(res["fixed"])
        self.assertIn("boom", res["reason"])
        self.assertEqual(open(self.path, "rb").read(), b"original")
        self.assertEqual(os.listdir(self.dir), ["a.mp4"])


    def _ok_proc(self):
        tmp = os.path.join(self.dir, "a.sync-tmp.mp4")

        class Proc:
            returncode = 0

            def __init__(self, cmd, **k):
                self.cmd = cmd
                Proc.last = cmd
                with open(tmp, "wb") as f:
                    f.write(b"neu")

            def communicate(self):
                return b"", b""

            def poll(self):
                return 0
        return Proc

    def test_implausible_result_keeps_original(self):
        res = {}
        probes = iter([{"duration": 60.0, "video": True, "audio": True},
                       {"duration": 5.0, "video": True, "audio": True}])
        t = s.SyncFixThread(self.path, None, 6.0, on_finish=res.update,
                            analyze=lambda p, crop, end: {"ok": True, "cut_s": 8.0, "lateness_ms": 100},
                            popen=self._ok_proc(), probe=lambda p: next(probes))
        t.start()
        t.join(5)
        self.assertFalse(res["fixed"])
        self.assertIn("unplausibel", res["reason"])
        self.assertEqual(open(self.path, "rb").read(), b"original")
        self.assertEqual(os.listdir(self.dir), ["a.mp4"])

    def test_success_replaces_and_adds_draw_delay(self):
        res = {}
        probes = iter([{"duration": 60.0, "video": True, "audio": True},
                       {"duration": 52.0, "video": True, "audio": True}])
        proc = self._ok_proc()
        with mock.patch.object(s, "get_ffmpeg_path", return_value="ffmpeg"):
            t = s.SyncFixThread(self.path, None, 6.0, on_finish=res.update, draw_delay_ms=40,
                                analyze=lambda p, crop, end: {"ok": True, "cut_s": 8.0, "lateness_ms": 100},
                                popen=proc, probe=lambda p: next(probes))
            t.start()
            t.join(5)
        self.assertTrue(res["fixed"])
        self.assertEqual(res["lateness_ms"], 140)
        second = proc.last.index("-ss", proc.last.index("-ss") + 1)
        self.assertEqual(proc.last[second + 1], "8.140000")
        self.assertEqual(open(self.path, "rb").read(), b"neu")
        self.assertEqual(os.listdir(self.dir), ["a.mp4"])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg fehlt")
class EndToEndTest(unittest.TestCase):
    """Erzeugte Aufnahme mit Marker + Kontrollereignissen, Ton zu spät/zu früh."""

    def _make(self, lag, rate, preset="ultrafast"):
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        gv = "gte(t,2.3)*lt(mod(t-2.3,2),0.15)*lt(t,20)"
        ga = f"gte(t,{2.3 + lag})*lt(mod(t-{2.3 + lag},2),0.12)*lt(t,{20 + lag})"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r={rate}:d=21",
             "-f", "lavfi", "-i",
             f"aevalsrc=exprs='0.4*sin(2*PI*1000*t)*{ga}+0.03*(random(0)-0.5)':s=48000:d=21",
             "-vf", ("drawbox=x=100:y=80:w=240:h=140:color=black:t=fill,"
                     f"drawbox=x=100:y=80:w=240:h=140:color=white:t=fill:enable='{gv}'"),
             "-c:v", "libx264", "-preset", preset, "-g", str(2 * rate),
             "-force_key_frames", "expr:lt(t,30)*gte(t,n_forced*0.5)",
             "-c:a", "aac", path],
            check=True,
        )
        return path

    def test_fix_and_recheck(self):
        crop = (108, 88, 224, 124)
        with mock.patch.object(s, "get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch.object(c, "get_ffmpeg_path", return_value="ffmpeg"):
            # veryfast/medium: mit B-Frames, wie in echten Aufnahmen
            for lag, rate, preset in ((0.81, 30, "veryfast"), (-0.2, 60, "ultrafast"),
                                      (0.2, 30, "medium")):
                path = self._make(lag, rate, preset)
                res = {}
                t = s.SyncFixThread(path, crop, 6.6, on_finish=res.update)
                t.start()
                t.join(60)
                self.assertTrue(res.get("fixed"), res)
                self.assertEqual(res["lateness_ms"], int(lag * 1000))
                check = c.analyze_file(path, crop, min_pairs=4)
                self.assertTrue(check["ok"], check)
                self.assertLessEqual(abs(check["lateness_ms"]), 20)


if __name__ == "__main__":
    unittest.main()
