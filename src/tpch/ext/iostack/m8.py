"""E1 fix, M8 / M9 analysis: PostgreSQL's light lineitem scan vs the cold full-pass pool replica, per run: clock, node-0
cores out of C6 and busy CPUs (job turbostat), CPU per read by process category (procmon), backend on-CPU share.
  PYTHONPATH=src .venv/bin/python -m tpch.ext.iostack.m8 [results/bench/<ts>_iostack_m8]
"""
from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path

from .procmon import window

NODE0_CORES = range(0, 16, 2)          # node 0's physical cores (CPU c, hyperthread sibling c + 16)


def turbostat_window(path: Path, t0: float, t1: float) -> tuple[float, float]:
    """(busy CPUs, cores out of C6) on node 0 between t0 and t1 (samples whose end lies inside)."""
    per = collections.defaultdict(list)
    for line in Path(path).read_text().splitlines():
        p = line.split("\t")
        if len(p) < 5 or p[0].startswith("Time") or p[1] == "-":
            continue
        try:
            t, cpu = float(p[0]), int(p[1])
        except ValueError:
            continue
        if t0 + 2 <= t <= t1:
            per[cpu].append((float(p[2]), float(p[5]) if len(p) > 5 and p[5] else None))
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    busy = sum(mean([x[0] for x in per[c]]) + mean([x[0] for x in per[c + 16]]) for c in NODE0_CORES) / 100
    awake = sum(100 - mean([x[1] for x in per[c] if x[1] is not None]) for c in NODE0_CORES) / 100
    return busy, awake


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(sorted(glob.glob("results/bench/*_iostack_m8"))[-1])
    ts, pm = out / "turbostat.txt", out / "procmon.jsonl"
    t_first = json.loads(pm.read_text().splitlines()[0])["t"]
    rows = []
    tag = "iostack_" + out.name.split("_")[-1] + "_"         # iostack_m8_ / iostack_m9_
    for d in sorted(glob.glob(f"results/runs/*_sf100_*{tag}*")):
        m = json.loads((Path(d) / "meta.json").read_text())
        s = json.loads((Path(d) / "summary.json").read_text())
        if m["t_start"] < t_first or not (out / "turbostat.txt").exists():
            continue
        n = s["dev_read_MiB"] / 0.125
        rows.append(("pg " + s["query"].replace("scan.lineitem_", "") + " " + s["label"][len(tag):], m["t_start"], m["t_end"], n, s["perf_GHz"],
                     s["perf_task_clock_s"] / s["wall_s"], s["perf_cycles"] * s["perf_user_cycles_frac"] / n, s["wall_s"]))
    cells = out / "cells.jsonl"
    for line in (cells.read_text().splitlines() if cells.exists() else []):
        c = json.loads(line)
        if "t0" not in c:                                    # time-window cells only (poolfull)
            continue
        rows.append(("rep " + c["cell"], c["t0"], c["t1"], c["reads"], c["GHz"], c["on_cpu"], c["user_cycles"] / c["reads"],
                     c["meas_s"]))
    print(f"{'run':28s} {'start':>8s} {'wall':>6s} {'GHz':>6s} {'D':>5s} {'u/read':>7s} {'busy':>5s} {'awake':>5s}  CPU us/read by category")
    for name, t0, t1, n, ghz, d, u, wall in sorted(rows, key=lambda r: r[1]):
        busy, awake = turbostat_window(ts, t0, t1)
        cat = window(pm, t0, t1)
        workers = {k: v for k, v in cat.items() if k.startswith("io_worker_")}
        if workers:
            cat["io_worker"] = sum(workers.values())
        cats = "  ".join(f"{k} {v / n * 1e6:.1f}" for k, v in sorted(cat.items()) if v / n * 1e6 >= 0.5)
        print(f"{name:28s} {t0 % 86400 / 3600 + 9:8.2f} {wall:6.1f} {ghz:6.3f} {d:5.3f} {u / 1e3:6.0f}k {busy:5.2f} {awake:5.2f}  {cats}")


if __name__ == "__main__":
    main()
