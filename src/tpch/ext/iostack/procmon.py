"""E1 fix (M8): CPU seconds per process category over time, for the whole machine, so a run's other-core
activity can be split by who does it (docs/ext/iostack.md, "Fix", M8).

Categories (by comm / cmdline): io_worker_N (postgres: io worker N, one per worker), kswapd (kswapd0/1, page reclaim),
pg_other (every other postgres process: backends with their io-wq threads, background processes),
replica (replica_pool / replica_io), kworker, monitor (this sampler), other. Per process, utime + stime from
/proc/<pid>/stat (it includes the process's exited threads); a process seen for the first time counts from then
on.

  procmon --out FILE [--interval S]      writes one JSON line per sample: {"t": epoch, "cpu": {cat: cumulative s}}
  window(FILE, t0, t1) -> {cat: CPU s between t0 and t1}
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

TCK = os.sysconf("SC_CLK_TCK")


def category(pid: str, comm: str) -> str:
    if int(pid) == os.getpid():
        return "monitor"
    if comm.startswith("kswapd"):
        return "kswapd"
    if comm.startswith("kworker"):
        return "kworker"
    if comm.startswith("replica_"):
        return "replica"
    if comm == "postgres":
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return "pg_other"
        if cmd.startswith(b"postgres: io worker"):          # io_worker_<N>: one per worker, summed by window()
            return "io_worker_" + cmd.split(b"\0")[0].split()[-1].decode()
        return "pg_other"
    return "other"


def sample(state: dict, total: dict) -> None:
    """state[pid] = (category, last cpu s); total[cat] accumulates deltas."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            continue
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        f = raw[raw.rindex(")") + 2:].split()
        cpu = (int(f[11]) + int(f[12])) / TCK
        start = f[19]                                       # starttime: a reused pid is a new process
        key = (pid, start)
        if key not in state:
            state[key] = (category(pid, comm), cpu)
            continue
        cat, last = state[key]
        total[cat] = total.get(cat, 0.0) + cpu - last
        state[key] = (cat, cpu)


def run(out: Path, interval: float) -> None:
    state: dict = {}
    total: dict = {}
    sample(state, total)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    with open(out, "a") as f:
        while not stop:
            time.sleep(interval)
            sample(state, total)
            f.write(json.dumps({"t": time.time(), "cpu": {k: round(v, 3) for k, v in total.items()}}) + "\n")
            f.flush()


def window(path: Path, t0: float, t1: float) -> dict:
    """CPU s per category between the samples nearest t0 and t1."""
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    a = min(rows, key=lambda r: abs(r["t"] - t0))
    b = min(rows, key=lambda r: abs(r["t"] - t1))
    return {k: b["cpu"].get(k, 0.0) - a["cpu"].get(k, 0.0) for k in set(a["cpu"]) | set(b["cpu"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    run(a.out, a.interval)


if __name__ == "__main__":
    sys.exit(main())
