"""E2 (parallel), M6 analysis: what the device sees in each regime (docs/ext/parallel.md, M6).

Reads <bench>/trace_*.csv (time s, task id, sector, sectors; one line per request issued to sdb) and prints, per
window of W seconds: MB/s, the share of requests that continue the previous request's sectors exactly
(sequential at the device), the switches between 64 MiB regions per request, and the share of the top issuing
task. A summary per trace splits the windows into fast (>= 900 MB/s) and slow (< 750 MB/s).
  PYTHONPATH=src .venv/bin/python -m tpch.ext.parallel.m6 [results/bench/<ts>_parallel_m6] [--window 5]
"""
from __future__ import annotations

import argparse
import collections
import glob
from pathlib import Path

REGION = 64 * 2**20 // 512          # 64 MiB in 512-byte sectors (a parallel-scan chunk)


def load(path: Path) -> list[tuple[float, int, int, int]]:
    rows = []
    for line in path.read_text().splitlines():
        p = line.split(",")
        if len(p) == 4 and p[2].isdigit():
            rows.append((float(p[0]), int(p[1]), int(p[2]), int(p[3])))
    return sorted(rows)


def windows(rows, width: float) -> list[dict]:
    out = []
    t0 = rows[0][0]
    groups = collections.defaultdict(list)
    for i, r in enumerate(rows):
        groups[int((r[0] - t0) // width)].append(i)
    for w in sorted(groups):
        idx = groups[w]
        seq = switches = 0
        tasks = collections.Counter()
        nbytes = 0
        for i in idx:
            t, task, sec, n = rows[i]
            nbytes += n * 512
            tasks[task] += 1
            if i > 0:
                pt, ptask, psec, pn = rows[i - 1]
                seq += sec == psec + pn
                switches += sec // REGION != psec // REGION
        k = len(idx)
        out.append({"t": w * width, "MBps": nbytes / 1e6 / width, "seq": seq / k, "switch": switches / k,
                    "top_task": tasks.most_common(1)[0][1] / k, "tasks": len(tasks), "n": k})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bench", nargs="?")
    ap.add_argument("--window", type=float, default=5)
    a = ap.parse_args()
    bench = Path(a.bench) if a.bench else Path(sorted(glob.glob("results/bench/*_parallel_m6"))[-1])
    for tr in sorted(bench.glob("trace_*.csv")):
        rows = load(tr)
        ws = [w for w in windows(rows, a.window) if w["n"] > 50]
        print(f"== {tr.stem}: {len(rows)} requests, {rows[-1][0] - rows[0][0]:.0f} s")
        for w in ws:
            print(f"  t {w['t']:5.0f}  {w['MBps']:6.0f} MB/s  seq {w['seq']:.2f}  region switches/req {w['switch']:.2f}  "
                  f"top task {w['top_task']:.2f} of {w['tasks']} tasks")
        for name, sel in (("fast >= 900", lambda w: w["MBps"] >= 900), ("slow < 750", lambda w: 300 < w["MBps"] < 750)):
            g = [w for w in ws if sel(w)]
            if g:
                m = lambda k: sum(w[k] for w in g) / len(g)
                print(f"  {name}: {len(g)} windows, {m('MBps'):.0f} MB/s, seq {m('seq'):.2f}, switches/req {m('switch'):.2f}")


if __name__ == "__main__":
    main()
