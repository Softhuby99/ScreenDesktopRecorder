import unittest

from audio_devices import audio_inputs_from_command, pick_default_render


class AudioInputsFromCommandTest(unittest.TestCase):
    def test_dshow_mic_without_prefix(self):
        cmd = ["ffmpeg", "-f", "dshow", "-thread_queue_size", "1024", "-audio_buffer_size", "20",
               "-i", "audio=Headset Microphone (Plantronics Blackwire 3220 Series)",
               "-f", "lavfi", "-i", "ddagrab=output_idx=0", "out.mp4"]
        self.assertEqual(audio_inputs_from_command(cmd),
                         ["Headset Microphone (Plantronics Blackwire 3220 Series)"])

    def test_pulse_and_x11grab(self):
        cmd = ["ffmpeg", "-f", "pulse", "-i", "alsa_input.pci.analog-stereo",
               "-f", "x11grab", "-i", ":0.0+11,178", "out.mp4"]
        self.assertEqual(audio_inputs_from_command(cmd), ["alsa_input.pci.analog-stereo"])

    def test_two_audio_inputs(self):
        cmd = ["-f", "dshow", "-i", "audio=Mic", "-f", "dshow", "-i", "audio=Stereomix",
               "-f", "gdigrab", "-i", "desktop"]
        self.assertEqual(audio_inputs_from_command(cmd), ["Mic", "Stereomix"])

    def test_no_audio_or_none(self):
        self.assertEqual(audio_inputs_from_command(["-f", "lavfi", "-i", "ddagrab"]), [])
        self.assertEqual(audio_inputs_from_command(None), [])


class PickDefaultRenderTest(unittest.TestCase):
    def test_newest_role0_among_active_wins(self):
        eps = [
            {"state": 1, "role0": (2026, 11, 15, 19, 0, 0, 0), "desc": "DELL S2725H", "iface": "Display Audio"},
            {"state": 1, "role0": (2026, 11, 15, 20, 57, 0, 0), "desc": "Lautsprecher", "iface": "Realtek(R) Audio"},
            {"state": 4, "role0": (2026, 12, 1, 0, 0, 0, 0), "desc": "Alt", "iface": "getrennt"},
            {"state": 1, "role0": None, "desc": "Nie Standard", "iface": "X"},
        ]
        self.assertEqual(pick_default_render(eps), "Lautsprecher (Realtek(R) Audio)")

    def test_nothing_usable(self):
        self.assertIsNone(pick_default_render([]))
        self.assertIsNone(pick_default_render([{"state": 1, "role0": None}]))


if __name__ == "__main__":
    unittest.main()
