"""
benchmark.py
============
Führt den automatischen Performance-Test in einem eigenen Thread aus,
damit die GUI zu keinem Zeitpunkt einfriert.

Ablauf:
  1. CPU-Messung (5 s): Testbilder werden im Echtzeit-Takt mit der
     anspruchsvollsten Einstellung (60 FPS, Preset 'medium') kodiert,
     dabei misst psutil im Sekundentakt die CPU-Auslastung.
     -> Entscheidungs-Matrix (< 60 % / 60-85 % / > 85 %) wie gehabt.
  2. Durchsatz-Kontrolle (je ~3 s): die von der Matrix gewählte
     Einstellung wird "so schnell wie möglich" kodiert. Schafft der
     Encoder die Bildrate NICHT mit Reserve, wird schrittweise ein
     schnelleres Preset bzw. eine niedrigere Bildrate gewählt.
  3. Ergebnis via Callback an den GUI-Thread zurückmelden.

Warum Schritt 2 nötig ist: eine niedrige CPU-Auslastung heißt nicht
automatisch, dass der Encoder schnell genug ist. Kommt er bei der
Aufnahme nicht hinterher, wird das Video nicht schlechter, sondern es
fehlen Bilder - es ruckelt. Genau das ist früher passiert, weil der Test
mit anderen Encoder-Einstellungen gemessen hat als die Aufnahme selbst.
Inzwischen nutzen beide exakt dieselben Parameter (siehe
ffmpeg_utils.build_video_encoder_args).

Es entsteht keine Testdatei - FFmpeg kodiert ins Nichts (-f null).
"""

import subprocess
import threading
import time

import psutil

from config import (
    BENCHMARK_DURATION,
    BENCHMARK_HEADROOM,
    BENCHMARK_MAX_PRESET_60FPS,
    BENCHMARK_PRESET_LADDER,
    BENCHMARK_SAMPLE_INTERVAL,
    BENCHMARK_THROUGHPUT_SECONDS,
    BENCHMARK_TIERS,
)
from ffmpeg_utils import build_benchmark_command
from platform_utils import get_subprocess_flags

# Die ersten Sekundenbruchteile eines Laufs (Prozessstart, Lookahead des
# Encoders fuellt sich) werden bei der Durchsatzberechnung ignoriert.
_WARMUP_SECONDS = 1.0


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
        try:
            self._emit(self.on_progress, "Starte Leistungstest ...")

            # ---- 1) CPU-Last bei der anspruchsvollsten Einstellung ------
            top = BENCHMARK_TIERS[0]
            stage = self._run_stage(
                build_benchmark_command(
                    preset=top["preset"], fps=top["fps"],
                    screen_size=self._screen_size, realtime=True,
                ),
                seconds=BENCHMARK_DURATION, sample_cpu=True,
                label="CPU-Messung",
            )
            samples = stage["cpu"] or [psutil.cpu_percent(interval=None)]
            avg_cpu = sum(samples) / len(samples)
            peak_cpu = max(samples)

            tier = next(t for t in BENCHMARK_TIERS if avg_cpu < t["max_cpu"])

            # ---- 2) Durchsatz der gewählten Einstellung prüfen ----------
            measured: dict[str, float] = {}

            def throughput_of(preset: str) -> float:
                if preset not in measured:
                    result = self._run_stage(
                        build_benchmark_command(
                            preset=preset, fps="30",
                            screen_size=self._screen_size, realtime=False,
                        ),
                        seconds=BENCHMARK_THROUGHPUT_SECONDS, sample_cpu=False,
                        label=f"Encoder-Test (Preset {preset})",
                    )
                    measured[preset] = result["fps"]
                return measured[preset]

            fps, preset, throughput, reachable = self.choose_config(tier, throughput_of)

            self._emit(self.on_progress, "Werte werden ausgewertet ...")
            result = self.build_result(
                fps, preset, throughput, reachable,
                avg_cpu=avg_cpu, peak_cpu=peak_cpu, samples=samples,
                measured=measured,
            )
            self._emit(self.on_finish, result)

        except InterruptedError:
            pass  # Abbruch ist kein Fehler
        except FileNotFoundError as exc:
            self._emit(self.on_error, str(exc))
        except Exception as exc:
            self._emit(self.on_error, f"Benchmark fehlgeschlagen: {exc}")
        finally:
            self._terminate_process()

    # ------------------------------------------------------------------
    # ENTSCHEIDUNG (rein rechnerisch - ohne FFmpeg testbar)
    # ------------------------------------------------------------------
    @staticmethod
    def choose_config(tier: dict, throughput_of) -> tuple[str, str, float, bool]:
        """
        Sucht ausgehend vom Matrix-Ergebnis (tier) die beste Einstellung,
        die der Encoder MIT RESERVE schafft.

        Reserve (BENCHMARK_HEADROOM): der Test misst nur das Kodieren.
        Bei der echten Aufnahme kommen Bildschirm abgreifen, Ton und
        alles, was sonst auf dem Rechner läuft, noch dazu.

        Reihenfolge: erst die Bildrate des Tiers mit immer schnelleren
        Presets, dann die nächstniedrigere Bildrate. 60 FPS werden nur
        mit höchstens BENCHMARK_MAX_PRESET_60FPS empfohlen - lieber 30
        FPS in guter Qualität als 60 FPS mit sehr grobem Bild.

        :param throughput_of: Funktion preset -> gemessene Bilder/s
        :return: (fps, preset, gemessener Durchsatz, ausreichend?)
        """
        ladder = list(BENCHMARK_PRESET_LADDER)
        start = ladder.index(tier["preset"]) if tier["preset"] in ladder else 0
        max_60 = ladder.index(BENCHMARK_MAX_PRESET_60FPS)

        tier_fps = float(tier["fps"])
        fps_candidates = [f for f in ("60", "30", "24") if float(f) <= tier_fps]

        for fps in fps_candidates:
            need = float(fps) * BENCHMARK_HEADROOM
            presets = ladder[start:]
            if fps == "60":
                presets = [p for p in presets if ladder.index(p) <= max_60]
            for preset in presets:
                value = throughput_of(preset)
                if value >= need:
                    return fps, preset, value, True

        # Nicht einmal das schnellste Preset reicht bei der niedrigsten
        # Bildrate - trotzdem die schonendste Einstellung empfehlen.
        fastest = ladder[-1]
        return fps_candidates[-1], fastest, throughput_of(fastest), False

    @staticmethod
    def build_result(fps: str, preset: str, throughput: float, reachable: bool,
                     avg_cpu: float, peak_cpu: float, samples: list,
                     measured: dict) -> dict:
        """Setzt Meldung/Farbe passend zur tatsächlich gewählten Einstellung."""
        if fps == "60":
            tier = BENCHMARK_TIERS[0]
        elif fps == "30" and preset in ("medium", "fast", "faster"):
            tier = BENCHMARK_TIERS[1]
        else:
            tier = BENCHMARK_TIERS[2]

        need = float(fps) * BENCHMARK_HEADROOM
        message = tier["message"]
        if fps == "24":
            message += "\n24 FPS gewählt - 30 FPS schafft dieses Gerät nicht ruckelfrei."
        if reachable:
            message += (f"\n(Encoder schafft {throughput:.0f} Bilder/s mit Preset "
                        f"'{preset}', benötigt werden {need:.0f}.)")
        else:
            message += (f"\n\nAchtung: Selbst mit dem schnellsten Preset schafft "
                        f"der Encoder nur {throughput:.0f} Bilder/s in voller "
                        f"Bildschirmauflösung. Für flüssige Aufnahmen besser "
                        f"\"Bereich wählen\" und einen kleineren Ausschnitt aufnehmen.")

        return {
            "avg_cpu": round(avg_cpu, 1),
            "peak_cpu": round(peak_cpu, 1),
            "samples": samples,
            "throughput_fps": round(throughput, 1),
            "measured": {k: round(v, 1) for k, v in measured.items()},
            "fps": fps,
            "encoder": tier["encoder"],
            "preset": preset,
            "title": tier["title"] if reachable else "Schwache Hardware",
            "message": message,
            "color": tier["color"],
        }

    # ------------------------------------------------------------------
    # EIN MESSLAUF
    # ------------------------------------------------------------------
    def _run_stage(self, cmd: list, seconds: float, sample_cpu: bool, label: str) -> dict:
        """
        Startet FFmpeg, lässt es 'seconds' Sekunden laufen und liefert
        {"fps": kodierte Bilder pro Sekunde, "cpu": [CPU-Werte]}.

        Die Bilder pro Sekunde werden aus dem Anstieg des Bildzählers
        NACH der Anlaufphase berechnet - Prozessstart und das Füllen des
        Encoder-Lookaheads würden das Ergebnis sonst nach unten verzerren.
        """
        if self._cancelled.is_set():
            raise InterruptedError("Benchmark abgebrochen.")

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **get_subprocess_flags(),
        )
        process = self._process

        points: list[tuple[float, int]] = []   # (Zeitpunkt, kodierte Bilder)
        stderr_lines: list[str] = []

        def read_progress():
            frame = 0
            try:
                for raw in iter(process.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line.startswith("frame="):
                        try:
                            frame = int(line.split("=", 1)[1])
                        except ValueError:
                            pass
                    elif line.startswith("progress="):
                        points.append((time.time(), frame))
            except Exception:
                pass

        def read_stderr():
            try:
                for raw in iter(process.stderr.readline, b""):
                    text = raw.decode("utf-8", errors="ignore").rstrip()
                    if text:
                        stderr_lines.append(text)
                        del stderr_lines[:-20]
            except Exception:
                pass

        readers = [
            threading.Thread(target=read_progress, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for t in readers:
            t.start()

        started = time.time()
        cpu_samples: list[float] = []
        if sample_cpu:
            psutil.cpu_percent(interval=None)  # Referenzpunkt setzen

        while time.time() - started < seconds:
            if self._cancelled.is_set():
                raise InterruptedError("Benchmark abgebrochen.")
            if process.poll() is not None:
                break
            if sample_cpu:
                # blockiert BENCHMARK_SAMPLE_INTERVAL Sekunden und liefert
                # den Durchschnitt dieses Zeitfensters
                value = psutil.cpu_percent(interval=BENCHMARK_SAMPLE_INTERVAL)
                cpu_samples.append(value)
                self._emit(
                    self.on_progress,
                    f"{label} ... {time.time() - started:.0f}s (CPU: {value:.0f} %)",
                )
            else:
                self._emit(self.on_progress, f"{label} ...")
                time.sleep(0.25)

        exited_early = process.poll() is not None
        self._terminate_process()
        for t in readers:
            t.join(timeout=2)

        if exited_early and process.returncode not in (0, None) and len(points) < 2:
            raise RuntimeError(
                "\n".join(stderr_lines[-5:])
                or "FFmpeg konnte den Leistungstest nicht ausführen."
            )

        return {"fps": self.throughput_from_points(points, started), "cpu": cpu_samples}

    @staticmethod
    def throughput_from_points(points: list[tuple[float, int]], started: float) -> float:
        """
        Bilder pro Sekunde aus den Fortschrittsmeldungen - als Anstieg
        zwischen erster Meldung nach der Anlaufphase und letzter Meldung.
        Gibt es dafür zu wenige Meldungen (sehr langsames Gerät), wird
        ersatzweise über die gesamte Laufzeit gemittelt.
        """
        if not points:
            return 0.0
        steady = [p for p in points if p[0] - started >= _WARMUP_SECONDS]
        if len(steady) >= 2 and steady[-1][0] > steady[0][0]:
            (t0, f0), (t1, f1) = steady[0], steady[-1]
            return max(0.0, (f1 - f0) / (t1 - t0))
        t_last, f_last = points[-1]
        return max(0.0, f_last / max(0.001, t_last - started))

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
