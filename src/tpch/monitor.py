"""Background samplers (iostat / mpstat / pidstat / perf stat) with a fixed, parseable output format.

The command lines here and the parsers in tpch.parse.sysstat form one contract:
change one only together with the other (and re-capture tests/fixtures).
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .shell import root_prefix

# C locale: '.' decimals and English headers. ISO time: iostat prints full timestamps.
SAMPLER_ENV = {"LC_ALL": "C", "S_TIME_FORMAT": "ISO"}


def iostat_cmd(device: str, interval: int = 1) -> list[str]:
    # -x extended, -t timestamp, -d devices only, -y skip the since-boot report
    return ["iostat", "-x", "-t", "-d", "-y", device, str(interval)]


def mpstat_cmd(interval: int = 1) -> list[str]:
    return ["mpstat", "-P", "ALL", str(interval)]


def pidstat_cmd(pid: int, interval: int = 1) -> list[str]:
    # -u CPU, -d disk, -h one line per sample, -H epoch-second timestamps
    return ["pidstat", "-u", "-d", "-h", "-H", "-p", str(pid), str(interval)]


def perf_stat_cmd(pid: int, events: str, interval_ms: int = 0) -> list[str]:
    """Counting mode on one process; CSV totals on stderr when perf gets SIGINT.

    Runs through sudo (hardware events need root). The shell prints its own PID and then
    exec's perf, so the first stdout line is perf's real PID: with sudo's `use_pty` there is
    an extra sudo monitor process and signals sent to sudo do not reach perf.
    """
    perf = ["env", "LC_ALL=C", "perf", "stat", "-x,", "-e", events, "-p", str(pid)]
    if interval_ms:   # one line per event per interval, first field the seconds since perf started
        perf[4:4] = ["-I", str(interval_ms)]
    return root_prefix() + ["sh", "-c", 'echo $$; exec "$@"', "sh"] + perf


def perf_record_cmd(pid: int, out: Path, freq: int) -> list[str]:
    """Sampling profile of one process: `cpu-clock` (a software timer, so the hardware counters stay free
    for perf stat), wall-clock timestamps (to split the samples at the phases of timeline.csv), no call
    graphs (overhead). Same PID trick as perf_stat_cmd."""
    perf = ["env", "LC_ALL=C", "perf", "record", "-q", "-e", "cpu-clock", "-F", str(freq), "-k", "CLOCK_REALTIME",
            "-p", str(pid), "-o", str(out)]
    return root_prefix() + ["sh", "-c", 'echo $$; exec "$@"', "sh"] + perf


class Monitors:
    """Run samplers in the background for the duration of a `with` block.

        with Monitors(out_dir, device="sda", pid=backend_pid):
            run_query()

    Raw output goes to <out_dir>/{iostat,mpstat,pidstat,perf}.log. Samplers are stopped
    even if the body raises. perf runs only with a pid and perf_events.
    """

    def __init__(self, out_dir: Path, device: str, pid: int | None = None, interval: int = 1,
                 perf_events: str | None = None, perf_interval_ms: int = 0, perf_record_hz: int = 0):
        self.out_dir = Path(out_dir)
        self.cmds = {"iostat": iostat_cmd(device, interval), "mpstat": mpstat_cmd(interval)}
        if pid is not None:
            self.cmds["pidstat"] = pidstat_cmd(pid, interval)
            if perf_events:
                self.cmds["perf"] = perf_stat_cmd(pid, perf_events, perf_interval_ms)
            if perf_record_hz:
                self.cmds["perf_record"] = perf_record_cmd(pid, self.out_dir / "perf.data", perf_record_hz)
        self.procs: list[subprocess.Popen] = []
        self.files = []
        self.perf: tuple[subprocess.Popen, int] | None = None   # (sudo process, perf pid)
        self.root_procs: list[tuple[subprocess.Popen, int]] = []  # every perf: (sudo process, perf pid)
        self.perf_t0: float | None = None   # wall time perf started (its interval times count from here)

    def __enter__(self) -> "Monitors":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **SAMPLER_ENV}
        for name, cmd in self.cmds.items():
            f = open(self.out_dir / f"{name}.log", "w")
            self.files.append(f)
            if name in ("perf", "perf_record"):   # stdout: perf's PID; stderr: the log file
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=f, text=True, env=env)
                line = p.stdout.readline().strip()
                if not line.isdigit():
                    p.kill()
                    raise RuntimeError(f"{name} did not start: {' '.join(cmd)}")
                self.root_procs.append((p, int(line)))
                if name == "perf":
                    self.perf = (p, int(line))
                    self.perf_t0 = time.time()   # perf exec'd right after printing its PID
            else:
                p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
            self.procs.append(p)
        return self

    def __exit__(self, *exc) -> None:
        root = {id(p): pid for p, pid in self.root_procs}
        for p, pid in self.root_procs:   # perf runs as root: signal it via sudo
            if p.poll() is None:
                subprocess.run(root_prefix() + ["kill", "-INT", str(pid)], check=False)
        for p in self.procs:
            if p.poll() is None and id(p) not in root:
                p.send_signal(signal.SIGINT)  # sysstat flushes its last sample on SIGINT
        for p in self.procs:
            try:
                p.wait(timeout=60 if id(p) in root else 5)   # perf record writes its file on exit
            except subprocess.TimeoutExpired:
                if id(p) in root:
                    subprocess.run(root_prefix() + ["kill", "-KILL", str(root[id(p)])], check=False)
                p.kill()
                p.wait()
        for f in self.files:
            f.close()
        data = self.out_dir / "perf.data"
        if "perf_record" in self.cmds and data.exists() and root_prefix():   # root-owned: make it ours
            subprocess.run(root_prefix() + ["chown", f"{os.getuid()}:{os.getgid()}", str(data)], check=False)
