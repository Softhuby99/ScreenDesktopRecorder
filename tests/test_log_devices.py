import unittest

from audio_devices import audio_inputs_from_command


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


if __name__ == "__main__":
    unittest.main()
