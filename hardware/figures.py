"""Device characterization: RAID controller cache and bandwidth vs physical position.

    hardware/report.sh        (the results it reads are listed there)

The location scan, the earlier scans it is compared with, the memory-latency run (`./tpch bench mem`)
and the controller-cache test are given as --input.
Writes fig1_location.png, fig2_memory.png and numbers.json into this directory.
"""
from __future__ import annotations

import json
import re
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.bench import mem
from tpch.bench.location import CACHE_HIT_MS, evicted, load_scan
from tpch.config import Config
from tpch.report import style
from tpch.report.data import need


def _color(target: str) -> str:
    if target.startswith("sf10."):
        return style.SERIES[0]
    if target.startswith("sf1."):
        return style.SERIES[1]
    return style.NEUTRAL


def _bw(rows: list[dict]) -> float | None:
    rows = [r for r in rows if not r["cache_hit"]]
    return sum(r["bytes"] for r in rows) / 2**20 / sum(r["seconds"] for r in rows) if rows else None


def _cache_test(cfg: Config, run: str) -> dict:
    log = cfg.repo / cfg.RESULTS_DIR / "bench" / run / "log.txt"
    steps = []
    for line in log.read_text().splitlines():
        m = re.match(r"(C\d)\s+(.*?)\s{2,}.*?([\d.]+) ([MG])B/s", line)
        if m:
            steps.append({"claim": m[1], "step": m[2], "MBps": float(m[3]) * (1000 if m[4] == "G" else 1)})
    return {"log": str(log.relative_to(cfg.repo)), "steps": steps}


def collect(cfg: Config, inputs: dict[str, list[str]] | None) -> dict:
    (scan,), earlier, (mem_run,), (cache_run,) = need(inputs, "scan", "earlier", "mem", "cache_test")
    bench = cfg.repo / cfg.RESULTS_DIR / "bench"
    main_path = bench / scan / "location.csv"
    assert evicted(main_path), f"{scan} was made without controller-cache eviction"
    paths = [bench / s / "location.csv" for s in earlier]
    main = load_scan(main_path)
    targets = list(dict.fromkeys(r["target"] for r in main))
    files = {t: {r["file"] for r in main if r["target"] == t} for t in targets}
    per_target = {}
    for t in targets:
        rs = [r for r in main if r["target"] == t]
        aw = [r["r_await_ms"] for r in rs]
        per_target[t] = {
            "MiBps": _bw(rs), "pieces": len(rs), "cache_hit_pieces": sum(r["cache_hit"] for r in rs),
            "piece_MiBps": {"median": st.median(r["MiBps"] for r in rs), "min": min(r["MiBps"] for r in rs),
                            "max": max(r["MiBps"] for r in rs)},
            "phys_GiB": {"min": min(r["phys_GiB"] for r in rs), "max": max(r["phys_GiB"] for r in rs),
                         "mean": sum(r["phys_GiB"] * r["bytes"] for r in rs) / sum(r["bytes"] for r in rs)},
            "r_await_ms": {"min": min(aw), "max": max(aw)},
            # earlier scans of the same files (the first two predate eviction)
            "earlier_MiBps": {p.parent.name: _bw([r for r in load_scan(p) if r["file"] in files[t]])
                              for p in paths},
        }
    return {"scan": main_path.parent.name, "cache_hit_ms": CACHE_HIT_MS, "targets": per_target,
            "earlier_scans_evicted": {p.parent.name: evicted(p) for p in paths},
            "controller_cache_test": _cache_test(cfg, cache_run), "memory": _memory(cfg, mem_run), "_main": main}


def _memory(cfg: Config, run: str) -> dict:
    path = cfg.repo / cfg.RESULTS_DIR / "bench" / run / "mem.csv"
    curve = mem.latency_curve(path)
    return {"run": path.parent.name, "ns_by_size_bytes": curve,
            "plateaus_ns": {p: {name: c[size] for name, size in
                                (("L1 (16 KiB)", 1 << 14), ("L2 (128 KiB)", 1 << 17), ("L3 (8 MiB)", 1 << 23),
                                 ("DRAM (1 GiB)", 1 << 30))} for p, c in curve.items()}}


def fig_memory(nums: dict, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.4))
    for p, color in (("local", style.SERIES[0]), ("remote", style.SERIES[1])):
        c = nums["memory"]["ns_by_size_bytes"][p]
        xs = sorted(c)
        ax.plot([x / 2**20 for x in xs], [c[x] for x in xs], "-o", color=color, markersize=4,
                label=f"memory on the {'same' if p == 'local' else 'other'} NUMA node ({p})")
    for size, name in ((32 / 1024, "L1 32 KiB"), (0.25, "L2 256 KiB"), (20, "L3 20 MiB")):
        ax.axvline(size, color=style.INK_2, linewidth=0.8, linestyle="--")
        ax.text(size * 1.08, 150, name, fontsize=8, color=style.INK_2)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("working-set size (MiB, log scale)")
    ax.set_ylabel("latency per dependent random load (ns, log)")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, borderaxespad=0.2)
    ax.set_title("Random-access latency: ~15-20 ns inside the LLC, 90-100 ns local DRAM, ~170 ns remote", pad=26)
    style.save(fig, out / "fig2_memory.png")


def fig_location(nums: dict, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.4))
    main = nums["_main"]
    for t, s in nums["targets"].items():
        rs = [r for r in main if r["target"] == t]
        ax.plot([r["phys_GiB"] for r in rs], [r["MiBps"] for r in rs], "o", color=_color(t), markersize=8,
                markeredgecolor=style.SURFACE, markeredgewidth=1.5, linestyle="none",
                label=f"{t}: {s['MiBps']:.1f} MiB/s")
    ax.set_xlim(0, 100)
    ax.set_ylim(160, 200)
    ax.set_xlabel("physical position of the 512 MiB piece in the volume (GiB from its start)")
    ax.set_ylabel("O_DIRECT sequential read, 128 KiB (MiB/s)")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, borderaxespad=0.2)
    ax.set_title("Sequential bandwidth falls toward the inner tracks (controller cache evicted first)", pad=26)
    style.save(fig, out / "fig1_location.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None,
          inputs: dict[str, list[str]] | None = None) -> dict:
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg, inputs)
    fig_location(nums, out)
    fig_memory(nums, out)
    public = {k: v for k, v in nums.items() if not k.startswith("_")}
    (out / "numbers.json").write_text(json.dumps(public, indent=1))
    return public
