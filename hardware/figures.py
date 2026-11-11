"""Device characterization: RAID controller cache and bandwidth vs physical position.

    ./tpch report hardware

Uses the newest location scan made with controller-cache eviction (`./tpch bench location`) and
compares it with every earlier scan, and the newest controller-cache test log.
Writes fig1_location.png and numbers.json into this directory.
"""
from __future__ import annotations

import json
import re
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.bench.location import CACHE_HIT_MS, evicted, load_scan, scans
from tpch.config import Config
from tpch.report import style


def _color(target: str) -> str:
    if target.startswith("sf10."):
        return style.SERIES[0]
    if target.startswith("sf1."):
        return style.SERIES[1]
    return style.NEUTRAL


def _bw(rows: list[dict]) -> float | None:
    rows = [r for r in rows if not r["cache_hit"]]
    return sum(r["bytes"] for r in rows) / 2**20 / sum(r["seconds"] for r in rows) if rows else None


def _cache_test(cfg: Config) -> dict | None:
    logs = sorted((cfg.repo / cfg.RESULTS_DIR / "bench").glob("*_controller_cache/log.txt"))
    if not logs:
        return None
    steps = []
    for line in logs[-1].read_text().splitlines():
        m = re.match(r"(C\d)\s+(.*?)\s{2,}.*?([\d.]+) ([MG])B/s", line)
        if m:
            steps.append({"claim": m[1], "step": m[2], "MBps": float(m[3]) * (1000 if m[4] == "G" else 1)})
    return {"log": str(logs[-1].relative_to(cfg.repo)), "steps": steps}


def collect(cfg: Config) -> dict:
    paths = scans(cfg)
    clean = [p for p in paths if evicted(p)]
    assert clean, "no location scan with controller-cache eviction: run ./tpch bench location"
    main_path = clean[-1]
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
                              for p in paths if p != main_path},
        }
    return {"scan": main_path.parent.name, "cache_hit_ms": CACHE_HIT_MS, "targets": per_target,
            "earlier_scans_evicted": {p.parent.name: evicted(p) for p in paths if p != main_path},
            "controller_cache_test": _cache_test(cfg), "_main": main}


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


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None) -> dict:
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg)
    fig_location(nums, out)
    public = {k: v for k, v in nums.items() if not k.startswith("_")}
    (out / "numbers.json").write_text(json.dumps(public, indent=1))
    return public
