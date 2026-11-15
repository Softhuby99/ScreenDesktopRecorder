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

    def test_marker_flashes_ignore_later_content(self):
        self.assertEqual(s.marker_flashes([2.0, 4.0, 6.0, 9.0], 6.2), [2.0, 4.0, 6.0])

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


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg fehlt")
class EndToEndTest(unittest.TestCase):
    """Erzeugte Aufnahme mit Marker + Kontrollereignissen, Ton zu spät/zu früh."""

    def _make(self, lag, rate):
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
             "-c:v", "libx264", "-preset", "ultrafast", "-g", str(2 * rate),
             "-force_key_frames", "expr:lt(t,30)*gte(t,n_forced*0.5)",
             "-c:a", "aac", path],
            check=True,
        )
        return path

    def test_fix_and_recheck(self):
        crop = (108, 88, 224, 124)
        with mock.patch.object(s, "get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch.object(c, "get_ffmpeg_path", return_value="ffmpeg"):
            for lag, rate in ((0.81, 30), (-0.2, 60)):
                path = self._make(lag, rate)
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
