"""
Gemeinsame Attrappen für die Tests: Fake-Popen, Fake-psutil-Prozess, Fake-Uhr.

Startet nie einen echten Prozess - kein FFmpeg, kein Display, keine
Audiohardware. Kein Testmodul (Dateiname passt nicht auf test*.py).
"""
import io
import subprocess
import threading


class FakeClock:
    """Austauschbare Uhr (Ersatz für time.monotonic)."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data
        if b"q" in data and self.proc.returncode is None and self.proc.factory.exit_on_q:
            self.proc.returncode = 0
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    """Verhält sich für die getesteten Pfade wie ein Popen-Objekt."""

    def __init__(self, factory, cmd, kwargs):
        self.factory = factory
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self.terminated = False
        self.communicate_calls = 0
        self.stdin = FakeStdin(self)
        self.stdout = io.BytesIO(factory.stdout_for(self.cmd))
        self.stderr = io.BytesIO(factory.stderr_data)

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout=None):
        if self.factory.on_wait is not None:
            self.factory.on_wait(self, timeout)
        if self.returncode is None:
            if self.factory.finish_on_wait:
                self.returncode = self.factory.returncode_on_finish
            else:
                raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.returncode

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self.factory.on_communicate is not None:
            self.factory.on_communicate(self)
        if (self.factory.timeout_first_communicate and self.communicate_calls == 1
                and not self.killed):
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if self.returncode is None:
            self.returncode = self.factory.returncode_on_finish
        if self.killed:
            return b"", b""
        return self.factory.stdout_for(self.cmd), self.factory.stderr_data


class PopenFactory:
    """
    Ersatz für subprocess.Popen (per unittest.mock.patch einsetzen).
    Alle erzeugten Prozesse stehen in .instances.
    """

    def __init__(self, stdout=b"", stderr=b"", on_create=None, on_communicate=None,
                 on_wait=None, finish_on_wait=True, returncode_on_finish=0,
                 exit_on_q=True, timeout_first_communicate=False):
        self._stdout = stdout
        self.stderr_data = stderr
        self.on_create = on_create
        self.on_communicate = on_communicate
        self.on_wait = on_wait
        self.finish_on_wait = finish_on_wait
        self.returncode_on_finish = returncode_on_finish
        self.exit_on_q = exit_on_q
        self.timeout_first_communicate = timeout_first_communicate
        self.instances: list[FakeProcess] = []
        self._lock = threading.Lock()

    def stdout_for(self, cmd) -> bytes:
        if callable(self._stdout):
            return self._stdout(cmd)
        return self._stdout

    def __call__(self, cmd, **kwargs):
        proc = FakeProcess(self, cmd, kwargs)
        with self._lock:
            self.instances.append(proc)
        if self.on_create is not None:
            self.on_create(proc)
        return proc


class _CpuTimes:
    def __init__(self, user):
        self.user = user
        self.system = 0.0


class FakePsProcess:
    """Ersatz für psutil.Process: zählt suspend()/resume()."""

    def __init__(self, active=True):
        self.suspend_calls = 0
        self.resume_calls = 0
        self.active = active
        self._cpu = 0.0
        self._lock = threading.Lock()

    def suspend(self):
        with self._lock:
            self.suspend_calls += 1

    def resume(self):
        with self._lock:
            self.resume_calls += 1

    def cpu_times(self):
        if self.active:
            self._cpu += 1.0       # arbeitet noch: Wert ändert sich
        return _CpuTimes(self._cpu)

    def io_counters(self):
        raise AttributeError("io_counters nicht verfügbar")


def progress_output(out_time_us, end=True, extra_blocks=1) -> bytes:
    """Erzeugt eine -progress-Ausgabe wie von ffmpeg (Blöcke, Ende mit progress=end)."""
    lines = []
    for i in range(extra_blocks):
        lines += [f"out_time_us={i * 1000}", f"out_time_ms={i * 1000}", "progress=continue"]
    lines += [
        "bitrate=N/A", "total_size=N/A",
        f"out_time_us={out_time_us}", f"out_time_ms={out_time_us}",
        "out_time=00:00:00.000000", "speed=N/A",
    ]
    if end:
        lines.append("progress=end")
    return ("\n".join(lines) + "\n").encode()
