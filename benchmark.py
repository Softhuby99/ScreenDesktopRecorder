"""
benchmark.py
============
Führt den automatischen Performance-Test in einem eigenen Thread aus,
damit die GUI zu keinem Zeitpunkt einfriert.

Ablauf:
  1. Unsichtbare 5-Sekunden-Testaufnahme ins TEMP-Verzeichnis
  2. Parallel: CPU-Messung im Sekundentakt via psutil
  3. Aufnahme stoppen, Testdatei löschen
  4. Mittelwert berechnen -> Tier aus BENCHMARK_TIERS ableiten
  5. Ergebnis via Callback an den GUI-Thread zurückmelden
"""

import os
import subprocess
import tempfile
import threading
import time
import uuid

import psutil

from config import (
    BENCHMARK_DURATION,
    BENCHMARK_SAMPLE_INTERVAL,
    BENCHMARK_TIERS,
)
from ffmpeg_utils import build_benchmark_command
from platform_utils import get_subprocess_flags


class BenchmarkThread(threading.Thread):
    """
    Worker-Thread für den Hardware-Benchmark.

    :param on_progress: Callback(text: str)    - Statusmeldungen
    :param on_finish:   Callback(result: dict) - Endergebnis
    :param on_error:    Callback(message: str) - Fehlerbehandlung
    """

    def __init__(self, on_progress=None, on_finish=None, on_error=None, screen_size=None):
        super().__init__(daemon=True)
        self.on_progress = on_progress
        self.on_finish = on_finish
        self.on_error = on_error
        # Vom GUI-Thread VOR dem Threadstart ermittelt - siehe
        # gui_main._start_benchmark() und den Kommentar bei RecorderThread.
        self._screen_size = screen_size
        self._process: subprocess.Popen | None = None
        self._cancelled = threading.Event()

    # ------------------------------------------------------------------
    def _emit(self, callback, *args):
        """Callback nur aufrufen, wenn gesetzt (defensive Programmierung)."""
        if callback:
            try:
                callback(*args)
            except Exception:
                pass

    def cancel(self):
        """Bricht den laufenden Benchmark ab (z. B. beim Schließen der App)."""
        self._cancelled.set()
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def run(self):
        temp_file = os.path.join(
            tempfile.gettempdir(), f"screenrec_bench_{uuid.uuid4().hex}.mp4"
        )

        try:
            self._emit(self.on_progress, "Starte Testaufnahme ...")

            cmd = build_benchmark_command(
                temp_file, BENCHMARK_DURATION, fps="30", screen_size=self._screen_size,
            )
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **get_subprocess_flags(),
            )

            # Kurz warten und prüfen, ob FFmpeg überhaupt starten konnte
            time.sleep(0.6)
            if self._process.poll() is not None:
                err = b""
                try:
                    err = self._process.stderr.read() or b""
                except Exception:
                    pass
                raise RuntimeError(
                    err.decode("utf-8", errors="ignore").strip()
                    or "FFmpeg konnte die Testaufnahme nicht starten."
                )

            # ----------------------------------------------------------
            # CPU-Messung im Sekundentakt
            # psutil.cpu_percent(interval=X) blockiert X Sekunden und
            # liefert den Durchschnitt dieses Zeitfensters.
            # ----------------------------------------------------------
            samples: list[float] = []
            psutil.cpu_percent(interval=None)  # Referenzpunkt setzen

            for i in range(BENCHMARK_DURATION):
                if self._cancelled.is_set():
                    raise InterruptedError("Benchmark abgebrochen.")

                value = psutil.cpu_percent(interval=BENCHMARK_SAMPLE_INTERVAL)
                samples.append(value)
                self._emit(
                    self.on_progress,
                    f"Messung läuft ... {i + 1}/{BENCHMARK_DURATION}s "
                    f"(CPU: {value:.0f} %)",
                )

            self._emit(self.on_progress, "Werte werden ausgewertet ...")
            self._terminate_process()

            if not samples:
                raise RuntimeError("Keine CPU-Messwerte erfasst.")

            avg_cpu = sum(samples) / len(samples)
            peak_cpu = max(samples)

            # Entscheidungs-Matrix anwenden
            tier = next(t for t in BENCHMARK_TIERS if avg_cpu < t["max_cpu"])

            result = {
                "avg_cpu": round(avg_cpu, 1),
                "peak_cpu": round(peak_cpu, 1),
                "samples": samples,
                "fps": tier["fps"],
                "encoder": tier["encoder"],
                "preset": tier["preset"],
                "title": tier["title"],
                "message": tier["message"],
                "color": tier["color"],
            }

            self._emit(self.on_finish, result)

        except InterruptedError:
            pass  # Abbruch ist kein Fehler
        except FileNotFoundError as exc:
            self._emit(self.on_error, str(exc))
        except Exception as exc:
            self._emit(self.on_error, f"Benchmark fehlgeschlagen: {exc}")
        finally:
            self._terminate_process()
            self._cleanup_temp(temp_file)

    # ------------------------------------------------------------------
    def _terminate_process(self):
        """Beendet FFmpeg zuerst freundlich ('q'), danach hart."""
        if not self._process or self._process.poll() is not None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.write(b"q")
                self._process.stdin.flush()
                self._process.stdin.close()
            self._process.wait(timeout=3)
        except Exception:
            try:
                self._process.kill()
                self._process.wait(timeout=2)
            except Exception:
                pass

    def _cleanup_temp(self, path: str):
        """Löscht die temporäre Testdatei (mit kurzen Retries)."""
        for _ in range(5):
            try:
                if os.path.exists(path):
                    os.remove(path)
                return
            except Exception:
                time.sleep(0.3)