"""E2 (parallel workers): Q6 / Q1 x SF100 with n parallel workers (docs/ext/parallel.md).

    ./tpch report ext/parallel

fig1_device      the array's read rate vs streams / participants: leapfrog, continuous streams, one deep stream,
                 and PostgreSQL's Q6 (M1, M2, M7, P1)
fig2_time        Q6 and Q1 time vs workers: measured and predicted, sync and io_uring (P1, P2)
fig3_clock       the clock with K busy cores (M1)
fig4_regimes     worker-mode I/O under a parallel scan: device rate and request order over one run (M6)
Writes fig1..fig4 and numbers.json into this directory. The runs and benches used are pinned below.
"""
from __future__ import annotations

import glob
import json
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.config import Config
from tpch.ext.parallel.m6 import load as load_trace, windows
from tpch.report import style

BLUE, ORANGE, AQUA = style.SERIES
M1 = "20261122-174727_parallel_m1"
M2 = "20261122-185424_parallel_m2"
M6 = "20261123-022212_parallel_m6"
M7 = "20261125-155744_parallel_m7"     # the scripted rerun of the 2026-11-23 hand-run probes (same results)
P1_RUNS = "20261122-18*_q6_sf100_plain_cold_parallel_{m}_n*"
P2_RUNS = "20261122-19*_q1_sf100_plain_cold_parallel_{m}_n*"
E1_ONE_BACKEND = {("q6", "sync"): 142.2, ("q6", "io_uring"): 121.2,      # docs/ext/iostack.md, medians of 3 runs
                  ("q1", "sync"): 702.2, ("q1", "io_uring"): 685.6}     # (Q1 io_uring: the held-out runs + diag)


def _runs(cfg: Config, pattern: str) -> list[tuple[dict, dict]]:
    root = cfg.repo / cfg.RESULTS_DIR / "runs"
    return [(json.loads((Path(d) / "meta.json").read_text()), json.loads((Path(d) / "summary.json").read_text()))
            for d in sorted(glob.glob(str(root / pattern)))]


def _cells(cfg: Config, name: str) -> list[dict]:
    return [json.loads(l) for l in (cfg.repo / cfg.RESULTS_DIR / "bench" / name / "cells.jsonl").read_text().splitlines()]


def collect(cfg: Config) -> dict:
    m1, m2, m7 = _cells(cfg, M1), _cells(cfg, M2), _cells(cfg, M7)
    nums: dict = {}
    cont = [c["MiBps"] for c in m7 if c["chunk_MiB"] == 0 and c["direct"] == 1]
    pg = sorted((int(m["settings"]["max_parallel_workers_per_gather"]) + 1, s["dev_avg_read_MiBps"])
                for m, s in _runs(cfg, P1_RUNS.format(m="sync")))
    nums["device"] = {
        "leapfrog_direct_qd1": sorted((c["P"], c["MiBps"]) for c in m2 if c["cell"].startswith("par:psync:1:")),
        "leapfrog_direct_qd16": sorted((c["P"], c["MiBps"]) for c in m2 if c["cell"].startswith("par:uring:1:")),
        "two_continuous_direct": {"median": st.median(cont), "min": min(cont), "max": max(cont)},
        "leapfrog_two_streams_m7": [c["MiBps"] for c in m7 if c["chunk_MiB"] == 64],
        "pg_q6_sync": pg}
    nums["time"] = {}
    model = cfg.repo / "model" / "ext" / "parallel"
    for q, pattern, pfile in (("q6", P1_RUNS, "q6_p1.json"), ("q1", P2_RUNS, "q1_p2.json")):
        pred = json.loads((model / pfile).read_text())["cells"]
        for m in ("sync", "io_uring"):
            meas = sorted((int(mm["settings"]["max_parallel_workers_per_gather"]), s["wall_s"])
                          for mm, s in _runs(cfg, pattern.format(m=m)) if s["label"] == f"parallel_{m}_n{mm['settings']['max_parallel_workers_per_gather']}")
            nums["time"][f"{q}_{m}"] = {"one_backend_s": E1_ONE_BACKEND[(q, m)], "measured": meas,
                                       "predicted": sorted((c["workers"], c["time_s"]) for c in pred if c["io_method"] == m)}
    nums["clock_busy_cores"] = sorted((int(c["cell"].split(":")[1]), c["GHz"]) for c in m1 if c["cell"].startswith("cores:"))
    nums["regimes"] = {}
    for tag in ("worker_off_1", "sync_off_1"):
        rows = load_trace(cfg.repo / cfg.RESULTS_DIR / "bench" / M6 / f"trace_{tag}.csv")
        ws = [w for w in windows(rows, 5.0) if w["n"] > 50]
        start = next(i for i, w in enumerate(ws) if w["MBps"] > 300 and w["t"] > 10)   # after the cold restart
        ws = ws[start:]
        nums["regimes"][tag] = [{"t": w["t"] - ws[0]["t"], "MBps": w["MBps"], "switch": w["switch"]} for w in ws]
    return nums


def fig_device(nums: dict, out: Path) -> None:
    d = nums["device"]
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.plot(*zip(*d["leapfrog_direct_qd1"]), "-o", color=BLUE,
            label="parallel-scan leapfrog, 1 read in flight per stream (O_DIRECT; P = 1: one sequential stream)")
    ax.plot(*zip(*d["leapfrog_direct_qd16"]), "--s", color=BLUE, mfc=style.SURFACE, mew=1.5,
            label="the same leapfrog, 16 reads in flight per stream (io_uring)")
    ax.plot(*zip(*d["pg_q6_sync"]), "-^", color=AQUA, ms=7, label="PostgreSQL Q6 with P participants (sync; device read rate)")
    c = d["two_continuous_direct"]
    ax.errorbar([2], [c["median"]], yerr=[[c["median"] - c["min"]], [c["max"] - c["median"]]], fmt="D", color=ORANGE,
                ms=7, capsize=4, label="two continuous streams, any gap from 8 to 96 MiB (O_DIRECT)")
    ax.set_xlabel("streams / participants P")
    ax.set_ylabel("device read rate (MiB/s)")
    ax.set_xticks(range(1, 9))
    ax.set_ylim(0, 2750)
    ax.set_title("The array's rate is set by the access pattern, not by the reads in flight")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=1, fontsize=8.5)
    style.save(fig, out / "fig1_device.png")


def fig_time(nums: dict, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    for ax, q in zip(axes, ("q6", "q1")):
        for m, color in (("sync", BLUE), ("io_uring", ORANGE)):
            t = nums["time"][f"{q}_{m}"]
            ax.plot(*zip(*t["predicted"]), "-", color=color, lw=1.5, alpha=0.9)
            ax.plot(*zip(*t["measured"]), "o", color=color, label=f"{m}: measured (dots), predicted (line)")
            ax.plot([0], [t["one_backend_s"]], "o", color=color, mfc=style.SURFACE, mew=1.5)
        ax.set_xlabel("parallel workers n (participants n + 1)")
        ax.set_ylabel("time (s)")
        ax.set_xticks(range(0, 8))
        ax.set_ylim(0, None)
        ax.set_title(f"{q.upper()} x SF100, buffered (open dots: one backend, E1)")
    axes[0].legend(loc="lower left", fontsize=8.5)
    fig.tight_layout()
    style.save(fig, out / "fig2_time.png")


def fig_clock(nums: dict, out: Path) -> None:
    k, f = zip(*nums["clock_busy_cores"])
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    ax.bar(k, f, width=0.6, color=BLUE, edgecolor=style.SURFACE, linewidth=2)
    for x, y in zip(k, f):
        ax.text(x, y - 0.12, f"{y:.2f}", ha="center", va="top", fontsize=8.5, color="white", fontweight="bold")
    ax.set_ylim(2.5, 3.6)
    ax.set_xlabel("busy cores on node 0 (pure computation)")
    ax.set_ylabel("clock (GHz)")
    ax.grid(axis="x", visible=False)
    ax.set_title("The clock with K busy cores follows the turbo bins")
    style.save(fig, out / "fig3_clock.png")


def fig_regimes(nums: dict, out: Path) -> None:
    fig, (a, b) = plt.subplots(2, 1, figsize=(7.2, 4.6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    for tag, color, label in (("worker_off_1", BLUE, "worker-mode I/O"), ("sync_off_1", ORANGE, "sync (control)")):
        r = nums["regimes"][tag]
        a.plot([w["t"] for w in r], [w["MBps"] for w in r], "-", color=color, label=label)
        b.plot([w["t"] for w in r], [w["switch"] for w in r], "-", color=color)
    a.set_ylabel("device MB/s")
    a.set_ylim(0, 1200)
    a.legend(loc="lower right", fontsize=8.5)
    a.set_title("Light scan, 2 participants: worker-mode I/O switches between two device regimes")
    b.set_ylabel("region switches\nper request")
    b.set_xlabel("seconds into the scan (5 s windows)")
    b.set_ylim(0, 1)
    fig.tight_layout()
    style.save(fig, out / "fig4_regimes.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None) -> dict:
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg)
    fig_device(nums, out)
    fig_time(nums, out)
    fig_clock(nums, out)
    fig_regimes(nums, out)
    (out / "numbers.json").write_text(json.dumps(nums, indent=1))
    return nums
