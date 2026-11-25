"""E4 (page cache): capacity, retention and write-back timing, Q3 x SF100 (docs/ext/cachesim.md).

    ./tpch report ext/cachesim

fig1_placement   page cache per NUMA node while a process bound to node 0 (left) or not bound (right) streams 45 GiB (M1)
fig2_retention   share of an 8 GiB file still cached after reading R GiB of other data, against LRU with node 0's capacity (M1)
fig3_q3          Q3 64MB with memory pinned on node 0: data read back from the device, and time, against the
                 capacity-only model and the final model (capacity + turnover) (M2, M4)
fig4_writeback   spill-file extents and read-back waiting under pinned memory, with and without a table scan turning
                 the cache over (M3)
Writes fig1..fig4 and numbers.json into this directory. The runs and benches used are pinned below.
"""
from __future__ import annotations

import csv
import glob
import json
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from tpch.config import Config
from tpch.model.data import Run
from tpch.report import style

BLUE, ORANGE, AQUA = style.SERIES
M1 = "20261123-151131_cachesim_m1"
M3 = "20261123-170821_cachesim_m3"
Q3_RUNS = "20261123-1[5-7]*_q3_sf100_plain_cold_cachesim_*_r0"      # M2 (24 GiB) and M4 (20 GiB)
WRITTEN_GIB = 8


def _bench(cfg: Config, name: str) -> Path:
    return cfg.repo / cfg.RESULTS_DIR / "bench" / name


def collect(cfg: Config) -> dict:
    nums: dict = {}
    m1 = _bench(cfg, M1)
    marks = {label: float(t) for t, label in csv.reader(open(m1 / "marks.csv"))}
    series: dict = {}
    for t, node, key, kb in csv.reader(open(m1 / "nodes.csv")):
        if key == "FilePages":
            series.setdefault(int(node), []).append((float(t), int(kb) / 2**20))
    nums["placement"] = {}
    for cell in ("place_bind", "place_nobind"):
        t0, t1 = marks[f"{cell} start"], marks[f"{cell} end"]      # the next cell's drop_caches follows the end mark
        nums["placement"][cell] = {f"node{n}": [(t - t0, g) for t, g in series[n] if t0 <= t <= t1] for n in (0, 1)}
    capacity_gib = max(g for _, g in series[0])
    nums["capacity_GiB"] = capacity_gib
    nums["retention"] = [{"read_GiB": int(r), "cached_share": int(res) / int(size)}
                         for r, res, size in csv.reader(open(m1 / "keep.csv"))]
    # Q3 with memory pinned on node 0
    model = cfg.repo / "model" / "ext" / "cachesim"
    cap24 = json.loads((model / "heldout_balloon.json").read_text())["cells"]
    fin20 = json.loads((model / "heldout_balloon20.json").read_text())["cells"]
    fin24 = json.loads((model / "seen_balloon24_final.json").read_text())["cells"]
    runs: dict = {}
    for d in sorted(glob.glob(str(cfg.repo / cfg.RESULTS_DIR / "runs" / Q3_RUNS))):
        d = Path(d)
        r = Run(d, json.loads((d / "meta.json").read_text()), json.loads((d / "summary.json").read_text()))
        pinned = 24 if r.meta["label"] == "cachesim_balloon" else 20 if r.meta["label"] == "cachesim_balloon20" else 0
        runs.setdefault(pinned, []).append({"wall_s": r.wall, "temp_dev_MiB": r.phases["p3_dev_read_MiB"]})
    nums["q3"] = {
        0: {"runs": runs[0], "capacity_only": {"time_s": cap24["no balloon"]["time_s"], "temp_dev_MiB": cap24["no balloon"]["temp_dev_MiB"]},
            "final": {"time_s": fin20["pinned_0"]["time_s"], "temp_dev_MiB": fin20["pinned_0"]["temp_dev_MiB"]}},
        20: {"runs": runs[20], "final": {"time_s": fin20["pinned_20"]["time_s"], "temp_dev_MiB": fin20["pinned_20"]["temp_dev_MiB"]},
             "held_out": True},
        24: {"runs": runs[24], "capacity_only": {"time_s": cap24["balloon 24 GiB"]["time_s"],
                                                 "temp_dev_MiB": cap24["balloon 24 GiB"]["temp_dev_MiB"]},
             "final": {"time_s": fin24["pinned_24"]["time_s"], "temp_dev_MiB": fin24["pinned_24"]["temp_dev_MiB"]}}}
    # M3: layout and read-back
    nums["writeback"] = {}
    for cell in ("b0_r450", "b16_r450", "b24_r450", "b24_r0"):
        r = json.loads((_bench(cfg, M3) / f"result_{cell}.json").read_text())
        nums["writeback"][cell] = {"extents_per_outer_file": sum(r["layout"]["outer_hist"]) / r["batches"],
                                   "readback_off_cpu_s": r["passes"][0]["off_cpu_s"]}
    nums["writeback_predicted_extents"] = {"b0_r450": 3.4, "b16_r450": 3.5, "b24_r450": 7.8, "b24_r0": 3.4}   # docs, M3
    return nums


def fig_placement(nums: dict, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.4), sharey=True)
    for ax, cell, title in zip(axes, ("place_bind", "place_nobind"), ("bound to node 0 (--membind=0)", "not bound")):
        for n, color in ((0, BLUE), (1, ORANGE)):
            ax.plot(*zip(*nums["placement"][cell][f"node{n}"]), "-", color=color, label=f"node {n}")
        ax.set_xlabel("seconds into the 45 GiB read")
        ax.set_title(title, fontsize=10)
    axes[0].set_ylabel("page cache (GiB)")
    axes[0].axhline(nums["capacity_GiB"], color=style.INK_2, lw=0.8, ls=":")
    axes[0].text(2, nums["capacity_GiB"] + 0.6, f"node 0 capacity {nums['capacity_GiB']:.1f} GiB", fontsize=8.5, color=style.INK_2)
    axes[0].legend(loc="lower right", fontsize=8.5)
    axes[0].set_ylim(0, 36)
    fig.suptitle("A bound process gets its own node's page cache, not the machine's", x=0.01, ha="left",
                 fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    style.save(fig, out / "fig1_placement.png")


def fig_retention(nums: dict, out: Path) -> None:
    c = nums["capacity_GiB"]
    xs = [x / 10 for x in range(0, 401)]
    lru = [min(1.0, max(0.0, (c - x) / WRITTEN_GIB)) * 100 for x in xs]
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.plot(xs, lru, "-", color=BLUE, label=f"LRU with node 0's capacity ({c:.1f} GiB)")
    pts = nums["retention"]
    ax.plot([p["read_GiB"] for p in pts], [p["cached_share"] * 100 for p in pts], "D", color=style.INK, ms=7,
            label="measured (fincore)")
    ax.set_xlabel("GiB of other data read after writing 8 GiB")
    ax.set_ylabel("% of the 8 GiB still cached")
    ax.set_ylim(-5, 110)
    ax.set_title("What the page cache keeps follows LRU with the node's capacity")
    ax.legend(loc="lower left", fontsize=8.5)
    style.save(fig, out / "fig2_retention.png")


def fig_q3(nums: dict, out: Path) -> None:
    labels = {0: "none", 20: "20 GiB\n(held out)", 24: "24 GiB\n(seen)"}
    keys = (0, 20, 24)
    fig, (a, b) = plt.subplots(1, 2, figsize=(9.6, 3.6))
    for ax, field, unit, title in ((a, "temp_dev_MiB", "MiB", "spill data read back from the device"),
                                   (b, "wall_s", "s", "time")):
        pkey = "temp_dev_MiB" if field == "temp_dev_MiB" else "time_s"
        for i, k in enumerate(keys):
            vals = [r[field] for r in nums["q3"][k]["runs"]]
            ax.bar(i, st.mean(vals), width=0.6, color=BLUE, edgecolor=style.SURFACE, linewidth=2)
            ax.plot([i] * len(vals), vals, "o", color=style.SURFACE, mec=BLUE, ms=5, zorder=3)
            if "capacity_only" in nums["q3"][k]:
                ax.plot(i + 0.18, nums["q3"][k]["capacity_only"][pkey], "o", ms=8, mfc="none", mec=style.INK_2, mew=1.5, zorder=4)
            ax.plot(i - 0.18, nums["q3"][k]["final"][pkey], "D", ms=6, color=style.INK, zorder=4)
        ax.set_xticks(range(3))
        ax.set_xticklabels([labels[k] for k in keys], fontsize=8.5)
        ax.set_xlabel("memory pinned on node 0", fontsize=8.5)
        ax.set_ylabel(unit)
        ax.grid(axis="x", visible=False)
        ax.set_title(title, fontsize=10)
    b.set_ylim(300, 420)
    fig.legend(handles=[Patch(color=BLUE, label="measured (dots: runs)"),
                        Line2D([], [], ls="", marker="o", mfc="none", mec=style.INK_2, mew=1.5, ms=8, label="capacity-only model"),
                        Line2D([], [], ls="", marker="D", color=style.INK, ms=6, label="final model (capacity + turnover)")],
               loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.06), fontsize=8.5)
    fig.suptitle("Q3 x SF100, 64MB: capacity predicts the data, write-back timing predicts the time", x=0.01, ha="left",
                 fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0.06, 1, 0.94))
    style.save(fig, out / "fig3_q3.png")


def fig_writeback(nums: dict, out: Path) -> None:
    cells = [("b0_r450", "none"), ("b16_r450", "16 GiB"), ("b24_r450", "24 GiB"), ("b24_r0", "24 GiB,\nno scan")]
    colors = [BLUE, BLUE, BLUE, ORANGE]
    fig, (a, b) = plt.subplots(1, 2, figsize=(9.6, 3.4))
    for ax, key, title, unit in ((a, "extents_per_outer_file", "extents per spill file", ""),
                                 (b, "readback_off_cpu_s", "read-back time off the CPU", " s")):
        ys = [nums["writeback"][c][key] for c, _ in cells]
        ax.bar(range(4), ys, width=0.6, color=colors, edgecolor=style.SURFACE, linewidth=2)
        for i, y in enumerate(ys):
            ax.text(i, y * 0.5, f"{y:.1f}{unit}", ha="center", va="center", fontsize=9, color="white", fontweight="bold")
        ax.set_xticks(range(4))
        ax.set_xticklabels([l for _, l in cells], fontsize=8.5)
        ax.set_xlabel("memory pinned on node 0", fontsize=8.5)
        ax.grid(axis="x", visible=False)
        ax.set_title(title, fontsize=10)
        ax.set_ylim(0, max(ys) * 1.25)
    a.plot(range(4), [nums["writeback_predicted_extents"][c] for c, _ in cells], "D", color=style.INK, ms=6, zorder=4)
    fig.legend(handles=[Patch(color=BLUE, label="table scan turning the cache over (450 MiB/s)"),
                        Patch(color=ORANGE, label="no turnover"),
                        Line2D([], [], ls="", marker="D", color=style.INK, ms=6, label="predicted, min(30 s, k C / R)")],
               loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.06), fontsize=8.5)
    fig.suptitle("A small page cache that is turned over starts write-back early and fragments the spill files",
                 x=0.01, ha="left", fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0.06, 1, 0.94))
    style.save(fig, out / "fig4_writeback.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None) -> dict:
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg)
    fig_placement(nums, out)
    fig_retention(nums, out)
    fig_q3(nums, out)
    fig_writeback(nums, out)
    (out / "numbers.json").write_text(json.dumps(nums, indent=1))
    return nums
