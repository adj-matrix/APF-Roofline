"""Background samplers (iostat / mpstat / pidstat / perf stat) with a fixed, parseable output format.

The command lines here and the parsers in tpch.parse.sysstat form one contract:
change one only together with the other (and re-capture tests/fixtures).
"""
from __future__ import annotations

import os
import signal
import subprocess
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


def perf_stat_cmd(pid: int, events: str) -> list[str]:
    """Counting mode on one process; CSV totals on stderr when perf gets SIGINT.

    Runs through sudo (hardware events need root). The shell prints its own PID and then
    exec's perf, so the first stdout line is perf's real PID: with sudo's `use_pty` there is
    an extra sudo monitor process and signals sent to sudo do not reach perf.
    """
    perf = ["env", "LC_ALL=C", "perf", "stat", "-x,", "-e", events, "-p", str(pid)]
    return root_prefix() + ["sh", "-c", 'echo $$; exec "$@"', "sh"] + perf


class Monitors:
    """Run samplers in the background for the duration of a `with` block.

        with Monitors(out_dir, device="sda", pid=backend_pid):
            run_query()

    Raw output goes to <out_dir>/{iostat,mpstat,pidstat,perf}.log. Samplers are stopped
    even if the body raises. perf runs only with a pid and perf_events.
    """

    def __init__(self, out_dir: Path, device: str, pid: int | None = None, interval: int = 1,
                 perf_events: str | None = None):
        self.out_dir = Path(out_dir)
        self.cmds = {"iostat": iostat_cmd(device, interval), "mpstat": mpstat_cmd(interval)}
        if pid is not None:
            self.cmds["pidstat"] = pidstat_cmd(pid, interval)
            if perf_events:
                self.cmds["perf"] = perf_stat_cmd(pid, perf_events)
        self.procs: list[subprocess.Popen] = []
        self.files = []
        self.perf: tuple[subprocess.Popen, int] | None = None   # (sudo process, perf pid)

    def __enter__(self) -> "Monitors":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **SAMPLER_ENV}
        for name, cmd in self.cmds.items():
            f = open(self.out_dir / f"{name}.log", "w")
            self.files.append(f)
            if name == "perf":   # stdout: perf's PID; stderr (the counts): the log file
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=f, text=True, env=env)
                line = p.stdout.readline().strip()
                if not line.isdigit():
                    p.kill()
                    raise RuntimeError(f"perf stat did not start: {' '.join(cmd)}")
                self.perf = (p, int(line))
            else:
                p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
            self.procs.append(p)
        return self

    def __exit__(self, *exc) -> None:
        if self.perf and self.perf[0].poll() is None:   # perf runs as root: signal it via sudo
            subprocess.run(root_prefix() + ["kill", "-INT", str(self.perf[1])], check=False)
        for p in self.procs:
            if p.poll() is None and (not self.perf or p is not self.perf[0]):
                p.send_signal(signal.SIGINT)  # sysstat flushes its last sample on SIGINT
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if self.perf and p is self.perf[0]:
                    subprocess.run(root_prefix() + ["kill", "-KILL", str(self.perf[1])], check=False)
                p.kill()
                p.wait()
        for f in self.files:
            f.close()
