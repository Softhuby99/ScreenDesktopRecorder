import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import date
from unittest import mock

import calibration as c


class SignalTest(unittest.TestCase):
    def test_beep_pcm_has_beeps_at_starts(self):
        pcm = c.make_beep_pcm(rate=8000)
        import array
        a = array.array("h")
        a.frombytes(pcm)
        starts = c.beep_start_samples(8000)
        self.assertEqual(len(starts), c.CALIB_BEEPS)
        for s in starts:
            seg = a[s:s + int(c.CALIB_BEEP_S * 8000)]
            self.assertGreater(max(abs(v) for v in seg), 10000)
            self.assertEqual(max(abs(v) for v in a[s - 400:s]), 0)


class EvaluateTest(unittest.TestCase):
    video = [3.0, 5.0, 7.0, 9.0, 11.0, 13.0]

    def test_late_audio_gives_negative_correction(self):
        audio = [t + 0.2 for t in self.video]
        r = c.evaluate(self.video, audio)
        self.assertTrue(r["ok"])
        self.assertEqual(r["lateness_ms"], 200)
        self.assertEqual(r["correction_ms"], -200)
        self.assertEqual(r["pairs"], 6)

    def test_rounding_to_10ms(self):
        r = c.evaluate(self.video, [t + 0.153 for t in self.video])
        self.assertEqual(r["correction_ms"], -150)

    def test_early_audio(self):
        r = c.evaluate(self.video, [t - 0.3 for t in self.video])
        self.assertEqual(r["correction_ms"], 300)

    def test_no_audio(self):
        r = c.evaluate(self.video, [])
        self.assertFalse(r["ok"])
        self.assertIn("Kein Piepton", r["reason"])

    def test_no_video(self):
        r = c.evaluate([], [1.0, 2.0])
        self.assertFalse(r["ok"])
        self.assertIn("Blinken", r["reason"])

    def test_too_few_pairs(self):
        r = c.evaluate(self.video[:3], [t + 0.1 for t in self.video[:3]])
        self.assertFalse(r["ok"])
        self.assertEqual(r["pairs"], 3)

    def test_spread_too_large(self):
        audio = [t + d for t, d in zip(self.video, [0.1, 0.2, 0.1, 0.2, 0.1, 0.2])]
        r = c.evaluate(self.video, audio)
        self.assertFalse(r["ok"])
        self.assertIn("unruhig", r["reason"])

    def test_far_away_beeps_are_not_paired(self):
        self.assertEqual(c.pair_offsets([3.0], [4.5]), [])


class OnsetTest(unittest.TestCase):
    def test_flash_onsets(self):
        frames = [(i / 30, 230.0 if (i // 15) % 4 == 1 else 16.0) for i in range(240)]
        self.assertEqual([round(t, 2) for t in c.flash_onsets(frames)], [0.5, 2.5, 4.5, 6.5])

    def test_flash_onsets_need_contrast(self):
        self.assertEqual(c.flash_onsets([(i / 30, 20.0 + i % 3) for i in range(90)]), [])

    def test_beep_onsets_and_snr(self):
        env = [5.0] * 400
        for start in (50, 250):
            for i in range(start, start + 12):
                env[i] = 800.0
        self.assertEqual([round(t, 2) for t in c.beep_onsets(env)], [0.5, 2.5])
        self.assertEqual(c.beep_onsets([5.0] * 400), [])

    def test_parse_showinfo(self):
        text = ("[Parsed_showinfo_2 @ 0x1] n:   0 pts:      0 pts_time:0       duration: 1 "
                "fmt:gray s:32x18 mean:[16] stdev:[0.0]\n"
                "[Parsed_showinfo_2 @ 0x1] n:   1 pts:    512 pts_time:0.0333  duration: 1 "
                "fmt:gray s:32x18 mean:[235] stdev:[0.0]\n")
        self.assertEqual(c.parse_showinfo(text), [(0.0, 16.0), (0.0333, 235.0)])


class StoreTest(unittest.TestCase):
    def test_remember_and_read(self):
        store = c.remember_offset({}, "Mic A", -200, c.SOURCE_CALIBRATED, today=date(2026, 11, 15))
        e = c.stored_offset(store, "Mic A")
        self.assertEqual(e, {"ms": -200, "source": "kalibriert", "date": "2026-11-15"})
        self.assertEqual(c.offset_note(e), "kalibriert am 15.11.2026")
        self.assertIsNone(c.stored_offset(store, "Mic B"))
        self.assertEqual(c.offset_note(None), "nicht kalibriert")

    def test_manual_note_and_clamp(self):
        store = {"Mic": {"ms": 5000, "source": "manuell"}}
        e = c.stored_offset(store, "Mic")
        self.assertEqual(e["ms"], 1000)
        self.assertEqual(c.offset_note(e), "manuell eingestellt")

    def test_garbage_is_ignored(self):
        self.assertIsNone(c.stored_offset({"Mic": "x"}, "Mic"))
        self.assertIsNone(c.stored_offset(None, "Mic"))
        self.assertIsNone(c.stored_offset({"Mic": {"ms": "abc"}}, "Mic"))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg fehlt")
class AnalyzeFileTest(unittest.TestCase):
    """End-to-end: erzeugte Datei mit bekanntem Versatz auswerten."""

    def _make(self, lag):
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        self.addCleanup(os.remove, path)
        gv = "gte(t,3.3)*lt(mod(t-3.3,2),0.15)*lt(t,15)"
        ga = f"gte(t,{3.3 + lag})*lt(mod(t-{3.3 + lag},2),0.12)*lt(t,{15 + lag})"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=160x90:r=30:d=17",
             "-f", "lavfi", "-i",
             f"aevalsrc=exprs='0.4*sin(2*PI*1000*t)*{ga}+0.02*(random(0)-0.5)':s=48000:d=17",
             "-vf", f"drawbox=color=white:t=fill:enable='{gv}'", "-c:v", "libx264",
             "-preset", "ultrafast", "-c:a", "aac", path],
            check=True,
        )
        return path

    def test_known_offsets(self):
        with mock.patch.object(c, "get_ffmpeg_path", return_value="ffmpeg"):
            for lag, expected in ((0.2, -200), (-0.3, 300)):
                r = c.analyze_file(self._make(lag))
                self.assertTrue(r["ok"], r)
                self.assertEqual(r["correction_ms"], expected)


class FakeProcess:
    def __init__(self, *a, **k):
        self.stdin = mock.MagicMock()
        self.stderr = mock.MagicMock()
        self.stderr.readline.return_value = b""
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakePlayer:
    def __init__(self, on_beep):
        self.on_beep = on_beep
        self.done = threading.Event()
        self.stopped = False

    def start(self):
        for i in range(c.CALIB_BEEPS):
            self.on_beep(100.0 + i)
        self.done.set()

    def stop(self):
        self.stopped = True


class WorkerTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(c, "CALIB_LEAD_S", 0.01),
            mock.patch.object(c, "CALIB_TAIL_S", 0.01),
            mock.patch.object(c, "build_record_command", return_value=["ffmpeg", "x"]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, analyze, **kw):
        events = {"flash": [], "finish": [], "error": []}
        t = c.CalibrationThread(
            "Mic", (0, 0, 100, 100), "30", (1920, 1080),
            on_flash=events["flash"].append, on_finish=events["finish"].append,
            on_error=events["error"].append,
            player_factory=FakePlayer, popen=FakeProcess, analyze=analyze, **kw,
        )
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive())
        return events

    def test_success_path(self):
        result = {"ok": True, "correction_ms": -200}
        ev = self._run(lambda path: result)
        self.assertEqual(ev["finish"], [result])
        self.assertEqual(len(ev["flash"]), c.CALIB_BEEPS)
        self.assertEqual(ev["error"], [])

    def test_temp_file_removed(self):
        seen = []
        self._run(lambda path: seen.append(path) or {"ok": False})
        self.assertFalse(os.path.exists(seen[0]))

    def test_error_reported(self):
        def boom(path):
            raise RuntimeError("kaputt")
        ev = self._run(boom)
        self.assertEqual(ev["finish"], [])
        self.assertIn("kaputt", ev["error"][0])

    def test_cancel_before_start_is_silent(self):
        t = c.CalibrationThread("Mic", (0, 0, 10, 10), "30", None,
                                on_finish=lambda r: self.fail("finish"),
                                on_error=lambda m: self.fail("error"),
                                player_factory=FakePlayer, popen=FakeProcess,
                                analyze=lambda p: {"ok": True})
        t.cancel()
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
