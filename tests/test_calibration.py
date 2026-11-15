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
    video = [3.0, 5.4, 7.8, 10.2, 12.6]

    def test_late_audio_gives_negative_correction(self):
        audio = [t + 0.2 for t in self.video]
        r = c.evaluate(self.video, audio)
        self.assertTrue(r["ok"])
        self.assertEqual(r["lateness_ms"], 200)
        self.assertEqual(r["correction_ms"], -200)
        self.assertEqual(r["pairs"], 5)

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
        audio = [t + d for t, d in zip(self.video, [0.1, 0.2, 0.1, 0.2, 0.1])]
        r = c.evaluate(self.video, audio)
        self.assertFalse(r["ok"])
        self.assertIn("unruhig", r["reason"])

    def test_far_away_beeps_are_not_paired(self):
        self.assertEqual(c.pair_offsets([3.0], [4.3]), [])


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

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


class FakePlayer:
    def __init__(self, on_beep, beeps=c.CALIB_BEEPS, interval=c.CALIB_INTERVAL_S):
        self.on_beep, self.beeps = on_beep, beeps
        self.done = threading.Event()
        self.device_info, self.latency_s, self.dac_beeps = "Fake [WASAPI]", 0.01, beeps

    def start(self):
        for i in range(self.beeps):
            self.on_beep(100.0 + i)
        self.done.set()

    def stop(self):
        self.done.set()


def run_result(lateness, applied=0, ok=True):
    r = {"ok": ok, "lateness_ms": lateness, "spread_ms": 10, "pairs": 5,
         "flash_times": [], "beep_times": [], "reason": "" if ok else "kaputt"}
    r["applied_ms"] = applied
    return r


class CombineRunsTest(unittest.TestCase):
    def test_stable_device(self):
        runs = [run_result(510), run_result(5, -510), run_result(-12, -510)]
        r = c.combine_runs(runs)
        self.assertTrue(r["ok"] and r["stable"])
        self.assertEqual(r["true_ms"], [510, 515, 498])
        self.assertEqual(r["correction_ms"], -510)
        self.assertEqual(r["rest_ms"], [0, 5, -12])

    def test_unstable_device(self):
        # wie das zweite Notebook: einmal 510, einmal 810 ms
        runs = [run_result(510), run_result(300, -510), run_result(0, -510)]
        r = c.combine_runs(runs)
        self.assertTrue(r["ok"])
        self.assertFalse(r["stable"])
        self.assertEqual(r["run_spread_ms"], 300)

    def test_too_few_good_runs(self):
        r = c.combine_runs([run_result(0, ok=False), run_result(100)])
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "kaputt")


class WorkerTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(c, "CALIB_LEAD_S", 0.01),
                  mock.patch.object(c, "CALIB_TAIL_S", 0.01),
                  mock.patch.object(c, "build_record_command",
                                    side_effect=lambda *a, **k: ["ffmpeg", str(k.get("audio_offset_ms"))])):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, analyze):
        ev = {"flash": [], "finish": [], "error": []}
        t = c.CalibrationThread(
            {"fps": "30", "audio_device": "Mic"}, (0, 0, 100, 100), log_dir=self.tmp,
            on_flash=ev["flash"].append, on_finish=ev["finish"].append,
            on_error=ev["error"].append, player_factory=FakePlayer,
            popen=FakeProcess, analyze=analyze,
        )
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive())
        return ev

    def test_three_runs_with_counter_check(self):
        applied = []
        lat = iter([200, 3, -4])

        def analyze(path, crop):
            return run_result(next(lat))
        with mock.patch.object(c, "build_record_command",
                               side_effect=lambda *a, **k: applied.append(k["audio_offset_ms"]) or ["x"]):
            ev = self._run(analyze)
        self.assertEqual(applied, [0, -200, -200])
        r = ev["finish"][0]
        self.assertTrue(r["ok"] and r["stable"])
        self.assertEqual(r["correction_ms"], -200)
        self.assertEqual(len(ev["flash"]), 3 * c.CALIB_BEEPS)
        log = open(r["log_path"], encoding="utf-8").read()
        self.assertIn("Durchlauf 3", log)
        self.assertIn("stabil", log)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".mp4")], [])

    def test_unstable_keeps_recordings(self):
        lat = iter([510, 300, 0])

        def analyze(path, crop):
            with open(path, "wb") as f:      # FFmpeg-Ersatz: Datei hat Inhalt
                f.write(b"x")
            return run_result(next(lat))
        ev = self._run(analyze)
        r = ev["finish"][0]
        self.assertFalse(r["stable"])
        self.assertIn("SCHWANKEND", open(r["log_path"], encoding="utf-8").read())
        self.assertEqual(len([f for f in os.listdir(self.tmp) if f.endswith(".mp4")]), 3)

    def test_two_failures_stop_early(self):
        calls = []
        ev = self._run(lambda p, crop: calls.append(1) or run_result(0, ok=False))
        self.assertEqual(len(calls), 2)
        self.assertFalse(ev["finish"][0]["ok"])

    def test_error_reported(self):
        def boom(path, crop):
            raise RuntimeError("kaputt")
        ev = self._run(boom)
        self.assertEqual(ev["finish"], [])
        self.assertIn("kaputt", ev["error"][0])

    def test_cancel_before_start_is_silent(self):
        t = c.CalibrationThread({"fps": "30"}, None,
                                on_finish=lambda r: self.fail("finish"),
                                on_error=lambda m: self.fail("error"),
                                player_factory=FakePlayer, popen=FakeProcess,
                                analyze=lambda p, crop: run_result(0))
        t.cancel()
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive())


class OutputFormatTest(unittest.TestCase):
    def test_stereo_pcm_is_interleaved(self):
        import array
        mono, stereo = array.array("h"), array.array("h")
        mono.frombytes(c.make_beep_pcm(rate=8000, beeps=2, interval=1.0))
        stereo.frombytes(c.make_beep_pcm(rate=8000, beeps=2, interval=1.0, channels=2))
        self.assertEqual(len(stereo), 2 * len(mono))
        self.assertEqual(list(stereo[0::2]), list(mono))
        self.assertEqual(list(stereo[1::2]), list(mono))

    def _fake_sd(self, wasapi_settings_ok=True):
        sd = mock.MagicMock()
        apis = [{"name": "MME", "default_output_device": 3},
                {"name": "Windows WASAPI", "default_output_device": 7}]
        sd.query_hostapis.side_effect = lambda i=None: apis if i is None else apis[i]

        def query_devices(dev=None, kind=None):
            if dev == 7:
                return {"name": "Lautsprecher", "default_samplerate": 44100.0,
                        "max_output_channels": 2, "hostapi": 1}
            return {"name": "Lautsprecher (MME)", "default_samplerate": 44100.0,
                    "max_output_channels": 2, "hostapi": 0}
        sd.query_devices.side_effect = query_devices
        if not wasapi_settings_ok:
            sd.WasapiSettings.side_effect = TypeError("unexpected keyword 'auto_convert'")
        return sd

    def test_attempts_use_device_format_first(self):
        sd = self._fake_sd()
        with mock.patch.dict("sys.modules", {"sounddevice": sd}):
            attempts = c.BeepPlayer.output_attempts(sd)
        self.assertEqual(attempts[0][:3], (7, 44100, 2))
        self.assertEqual(attempts[0][4], "WASAPI")
        self.assertEqual(attempts[1][1:3], (48000, 1))
        self.assertEqual(attempts[2][0], None)

    def test_missing_auto_convert_is_reported(self):
        sd = self._fake_sd(wasapi_settings_ok=False)
        with mock.patch.dict("sys.modules", {"sounddevice": sd}):
            attempts = c.BeepPlayer.output_attempts(sd)
        self.assertEqual(attempts[1][0], "skip")
        self.assertIn("auto_convert", attempts[1][4])

    def test_failed_wasapi_falls_back_and_logs_reason(self):
        sd = self._fake_sd()
        opened = []

        def raw_stream(**kw):
            if kw["device"] == 7:
                raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")
            opened.append(kw)
            st = mock.MagicMock()
            st.latency = 0.09
            st.device = 3
            return st
        sd.RawOutputStream.side_effect = raw_stream
        with mock.patch.dict("sys.modules", {"sounddevice": sd}):
            p = c.BeepPlayer(lambda due: None)
            p.start()
        self.assertEqual(opened[0]["channels"], 2)
        self.assertEqual(opened[0]["samplerate"], 44100)
        self.assertEqual(len(p.errors), 2)
        self.assertIn("-9997", p.errors[0])
        self.assertIn("MME", p.device_info)

    def test_callback_stereo_and_dac_time(self):
        sd = self._fake_sd()
        beeps = []
        with mock.patch.dict("sys.modules", {"sounddevice": sd}):
            p = c.BeepPlayer(beeps.append, beeps=2, interval=1.0)
            p._prepare(8000, 2)
            out = bytearray(2 * 2 * 4000)
            ti = mock.Mock(outputBufferDacTime=10.05, currentTime=10.0)
            p._callback(out, 4000, ti, None)    # erster Ton bei Frame 2400
        self.assertEqual(len(beeps), 1)
        self.assertEqual(p.dac_beeps, 1)
        self.assertEqual(bytes(out), p._pcm[:len(out)])


if __name__ == "__main__":
    unittest.main()
