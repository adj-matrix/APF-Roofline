"""Dependent random-access latency vs working-set size: what a hash-table probe costs when its
bucket and tuple are (not) in cache. A property of the machine, not of a query.

memlat.c (pointer chase over one random cycle of 64-byte lines) runs once per (placement, size,
repetition). Placement: CPU on NUMA node 0 with memory on node 0 (local) or node 1 (remote);
a PostgreSQL backend is not pinned, so its private memory may be either. CPU only, no disk I/O.

    ./tpch bench mem [--reps 3]          -> results/bench/<stamp>_mem/{mem.csv, meta.json}
"""
from __future__ import annotations

import csv
import json
import statistics as st
from datetime import datetime
from pathlib import Path

from .. import sysinfo
from ..config import Config
from ..shell import Shell

SRC = Path(__file__).with_name("memlat.c")
PLACEMENTS = {"local": ("0", "0"), "remote": ("0", "1")}     # (cpunodebind, membind)


def default_sizes() -> list[int]:
    """4 KiB .. 1 GiB: powers of two and the midpoints 1.5 x 2^k."""
    out = []
    for k in range(12, 31):
        out.append(1 << k)
        if k < 30:
            out.append(3 << (k - 1))
    return out


def accesses_for(size: int) -> int:
    """Enough dependent loads for ~0.5-1 s at DRAM latency, and at least 4 passes over the buffer."""
    return max(5_000_000, 4 * size // 64)


def memlat_cmd(binary: Path, placement: str, size: int) -> list[str]:
    cpu, mem = PLACEMENTS[placement]
    return ["numactl", f"--cpunodebind={cpu}", f"--membind={mem}", str(binary), str(size), str(accesses_for(size))]


def run_mem_bench(cfg: Config, reps: int = 3, sizes: list[int] | None = None,
                  dry_run: bool = False) -> tuple[Path, Shell]:
    sizes = sizes or default_sizes()
    sh = Shell(dry_run)
    out = cfg.repo / cfg.RESULTS_DIR / "bench" / f"{datetime.now():%Y%m%d-%H%M%S}_mem"
    binary = out / "memlat"
    if not dry_run:
        out.mkdir(parents=True)
    sh.run(["gcc", "-O2", "-o", str(binary), str(SRC)])
    rows = []
    for rep in range(reps):                  # repetitions outermost: drift spreads over all sizes
        for placement in PLACEMENTS:
            for size in sizes:
                txt = sh.run(memlat_cmd(binary, placement, size))
                if dry_run:
                    continue
                ns = float(txt.split()[1])
                rows.append({"placement": placement, "size_bytes": size, "rep": rep, "ns": ns})
                print(f"{placement:6s} {size / 2**20:10.3f} MiB  {ns:7.2f} ns", flush=True)
    if not dry_run:
        with open(out / "mem.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["placement", "size_bytes", "rep", "ns"])
            w.writeheader()
            w.writerows(rows)
        (out / "meta.json").write_text(json.dumps({
            "source": str(SRC.relative_to(cfg.repo)), "reps": reps, "placements": PLACEMENTS,
            "sys": sysinfo.collect()}, indent=2))
    return out, sh


def latest(cfg: Config) -> Path:
    return sorted((cfg.repo / cfg.RESULTS_DIR / "bench").glob("*_mem/mem.csv"))[-1]


def latency_curve(path: Path) -> dict[str, dict[int, float]]:
    """{placement: {size_bytes: median ns}}."""
    acc: dict[tuple[str, int], list[float]] = {}
    for r in csv.DictReader(open(path)):
        acc.setdefault((r["placement"], int(r["size_bytes"])), []).append(float(r["ns"]))
    out: dict[str, dict[int, float]] = {}
    for (p, s), xs in sorted(acc.items()):
        out.setdefault(p, {})[s] = st.median(xs)
    return out


def latency_at(curve: dict[int, float], size: int) -> float:
    """Latency at a working-set size, interpolated linearly in log(size)."""
    import math
    pts = sorted(curve.items())
    if size <= pts[0][0]:
        return pts[0][1]
    for (s0, l0), (s1, l1) in zip(pts, pts[1:]):
        if s0 <= size <= s1:
            f = (math.log(size) - math.log(s0)) / (math.log(s1) - math.log(s0))
            return l0 + f * (l1 - l0)
    return pts[-1][1]
