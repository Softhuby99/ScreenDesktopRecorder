"""
audio_meter.py
==============
Leichter Echtzeit-Pegelmesser (RMS + Peak) für die Mikrofon-/Lautsprecher-
Vorschau in der GUI. Komplett unabhängig vom FFmpeg-Aufnahmepfad - öffnet
nur eine kurze sounddevice-Vorschau rein zur Anzeige/Geräteauswahl.

Bewusst OHNE NumPy: sounddevice liefert die Audiodaten dann als
plattformneutrale Bytes/Buffer, was für die paar hundert Samples pro
Callback völlig ausreichend performant ist und keine zusätzliche (bei
PyInstaller nicht ganz kleine) Abhängigkeit erzwingt.
"""

import array
import math
import threading

FLOOR_DB = -60.0  # Pegel unterhalb dieser Schwelle gelten als "Stille" (0.0)


def compute_level(raw_bytes: bytes) -> tuple[float, float]:
    """
    Reine Rechenfunktion (keine Audio-Hardware nötig - gut testbar):
    nimmt einen Block 32-bit-Float-PCM-Samples entgegen und liefert
    (rms, peak), jeweils bereits auf eine 0.0-1.0-Anzeigeskala (dBFS,
    bei FLOOR_DB abgeschnitten) umgerechnet.
    """
    try:
        samples = array.array("f")
        samples.frombytes(raw_bytes)
    except Exception:
        return 0.0, 0.0

    if not samples:
        return 0.0, 0.0

    sum_squares = 0.0
    peak_amp = 0.0
    for s in samples:
        sum_squares += s * s
        a = s if s >= 0.0 else -s
        if a > peak_amp:
            peak_amp = a

    rms_amp = math.sqrt(sum_squares / len(samples))
    return _amplitude_to_unit(rms_amp), _amplitude_to_unit(peak_amp)


def _amplitude_to_unit(amplitude: float) -> float:
    """Lineare Amplitude (0.0-1.0) -> 0.0-1.0-Anzeigewert auf dBFS-Skala."""
    if amplitude <= 0.0:
        return 0.0
    db = 20.0 * math.log10(min(amplitude, 1.0))
    if db <= FLOOR_DB:
        return 0.0
    return max(0.0, min(1.0, (db - FLOOR_DB) / (0.0 - FLOOR_DB)))


class LevelMeter:
    """
    Öffnet einen sounddevice-InputStream für EIN Gerät und hält den
    zuletzt gemessenen Pegel thread-sicher vor. Die GUI fragt den Wert
    per Timer (self.after(...)) ab - keine direkte Tkinter-Kopplung,
    damit dieses Modul auch ohne laufende GUI test- und wiederverwendbar
    bleibt.

    WICHTIG: start() gibt bei jedem Fehler (kein Gerät, Gerät belegt,
    keine Audio-Hardware im System, ...) sauber False zurück, statt eine
    Exception hochzureichen - die GUI zeigt dann einen Hinweistext statt
    eines Balkens an, anstatt abzustürzen.
    """

    def __init__(self, device_index: int, max_channels: int = 1):
        self.device_index = device_index
        self._max_channels = max_channels
        self._stream = None
        self._lock = threading.Lock()
        self._rms = 0.0
        self._peak = 0.0
        self.error: str | None = None

    def start(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as exc:
            self.error = f"sounddevice nicht verfügbar: {exc}"
            return False

        try:
            info = sd.query_devices(self.device_index)
            channels = max(1, min(self._max_channels, int(info.get("max_input_channels", 1) or 1)))
            samplerate = int(info.get("default_samplerate", 44100) or 44100)

            self._stream = sd.InputStream(
                device=self.device_index,
                channels=channels,
                samplerate=samplerate,
                dtype="float32",
                blocksize=0,
                callback=self._callback,
            )
            self._stream.start()
            return True
        except Exception as exc:
            self.error = str(exc)
            self._stream = None
            return False

    def _callback(self, indata, frames, time_info, status):
        try:
            raw = bytes(indata)
        except Exception:
            return

        rms, peak = compute_level(raw)
        with self._lock:
            self._rms = rms
            self._peak = peak

    def get_level(self) -> tuple[float, float]:
        """:return: (rms 0..1, peak 0..1) - bereits dBFS-skaliert fürs Zeichnen."""
        with self._lock:
            return self._rms, self._peak

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
