"""E1 (I/O stack): io_method x io_direct under one backend, Q6 / Q1 x SF100 (docs/ext/iostack.md).

    ./tpch report ext/iostack

fig1_cells       the cells: measured time against the first prediction (v1) and the final model
fig2_errors      prediction error per condition, v1 and final, seen cells and held-out conditions
fig3_clock       the clock where sync / direct sits: replicas with fixed time vs fixed cycles per read, and PostgreSQL
fig4_family      worker / buffered: PostgreSQL's TPC-H-free scans vs the pool replica, and Q6 (model v3's source)
Writes fig1..fig4 and numbers.json into this directory. The runs and benches used are pinned below.
"""
from __future__ import annotations

import glob
import json
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from tpch.config import Config
from tpch.report import style

BLUE, ORANGE, AQUA = style.SERIES
E1_RUNS = "2026111*_{q}_sf100_plain_cold_iostack_*"            # the cells, 2026-11-19, 3 runs each
HELDOUT_RUNS = "20261120-03*_{q}_sf100_plain_cold_iostack_*"    # held-out, 2026-11-20, 2 runs each
M7 = ["20261120-002725_iostack_m7", "20261120-014441_iostack_m7b", "20261120-022430_iostack_m7c"]   # fixed time
M7D = "20261120-024700_iostack_m7d"                            # pool replica (worker mode)
M9 = "20261122-155305_iostack_m9"                              # fixed cycles (and a fixed-time control)
SCANS = "2026112[2]-*_scan.lineitem_*_sf100_plain_cold_iostack_m*"  # TPC-H-free scans, M8-M11
Q6_SAME_PERIOD = "20261122-17*_q6_sf100_plain_cold_iostack_m11_worker_off_r0"
CELLS = {"q6": [(m, d) for m in ("sync", "worker", "io_uring") for d in ("off", "data")],
         "q1": [("sync", "off"), ("sync", "data"), ("worker", "off"), ("io_uring", "data")]}
READ_MIB = 0.125


def _summaries(cfg: Config, pattern: str) -> list[dict]:
    root = cfg.repo / cfg.RESULTS_DIR / "runs"
    return [json.loads((Path(d) / "summary.json").read_text()) for d in sorted(glob.glob(str(root / pattern)))]


def _cells(cfg: Config, name: str) -> list[dict]:
    return [json.loads(l) for l in (cfg.repo / cfg.RESULTS_DIR / "bench" / name / "cells.jsonl").read_text().splitlines()]


def _u(s: dict) -> float:
    return s["perf_cycles"] * s["perf_user_cycles_frac"] / (s["dev_read_MiB"] / READ_MIB)


def _label(m: str, d: str) -> str:
    return f"{m} / {'buffered' if d == 'off' else 'direct'}"


def collect(cfg: Config) -> dict:
    nums: dict = {"cells": {}, "heldout": {}}
    model = cfg.repo / "model" / "ext" / "iostack"
    for q in ("q6", "q1"):
        runs: dict = {}
        for s in _summaries(cfg, E1_RUNS.format(q=q)):
            runs.setdefault(s["label"][len("iostack_"):], []).append(s["wall_s"])
        v1 = {(c["io_method"], c["io_direct"]): c["time_s"] for c in json.loads((model / f"{q}_sf100.json").read_text())["cells"]}
        final = {(c["io_method"], c["io_direct"]): c["time_s"]
                 for c in json.loads((model / "v3" / f"seen_{q}.json").read_text())["cells"]}
        nums["cells"][q] = [{"cell": _label(m, d), "measured_s": st.median(runs[f"{m}_{d}"]), "runs": runs[f"{m}_{d}"],
                             "v1_s": v1[(m, d)], "final_s": final[(m, d)]} for m, d in CELLS[q]]
    # worker / buffered drifts ~4 % between days: judged against Q6 runs of the same period as its scans (M11)
    same = [s["wall_s"] for s in _summaries(cfg, Q6_SAME_PERIOD)]
    nums["q6_worker_off_same_period_s"] = same
    for q in ("q6", "q1"):
        pred = json.loads((model / "v2" / f"heldout_{q}.json").read_text())["cells"]
        meas: dict = {}
        for s in _summaries(cfg, HELDOUT_RUNS.format(q=q)):
            meas.setdefault(s["label"][len("iostack_"):], []).append(s["wall_s"])
        for c in pred:
            w = c["extra"].get("io_workers")
            key = f"{c['io_method']}_{c['io_direct']}" + (f"_w{w}" if w and q == "q6" else "")
            if key in meas:
                nums["heldout"][f"{q.upper()} {_label(c['io_method'], c['io_direct'])}" + (f", {w} I/O worker" if w else "")] = {
                    "measured_s": st.median(meas[key]), "runs": meas[key], "pred_s": c["time_s"]}
    # the clock at sync / direct: replicas (fixed time, fixed cycles) and PostgreSQL
    fixed_time = [c for name in M7 for c in _cells(cfg, name) if c["cell"].split(":")[0] in ("io", "li-io")
                  and c["cell"].split(":")[1:4] == ["psync", "1", "1"] and not c["cell"].endswith("k")]
    fixed_time += [c for c in _cells(cfg, M9) if c["cell"] == "li-io:psync:1:1:200"]
    fixed_cyc = [c for c in _cells(cfg, M9) if c["cell"].startswith("li-io:psync:1:1:") and c["cell"].endswith("k")]
    pt = lambda c: [c["user_cycles"] / c["reads"] / 1e3, c["GHz"]]
    nums["clock"] = {"fixed_time": [pt(c) for c in fixed_time], "fixed_cycles": [pt(c) for c in fixed_cyc],
                     "pg_q6": [[_u(s) / 1e3, s["perf_GHz"]] for s in _summaries(cfg, E1_RUNS.format(q="q6"))
                               if s["label"] == "iostack_sync_data"],
                     "pg_scans": [[_u(s) / 1e3, s["perf_GHz"]] for s in _summaries(cfg, SCANS) if s["label"].endswith("_sync_data")]}
    # worker / buffered: PostgreSQL's scans, Q6, and the pool replica
    pool = [c for c in _cells(cfg, M7D) if c["cell"].startswith("pool:0:3:140:")]       # Q6's work per read
    nums["family"] = {"pg_scans": [[_u(s) / 1e3, s["perf_GHz"]] for s in _summaries(cfg, SCANS) if s["label"].endswith("_worker_off")],
                      "q6": [[_u(s) / 1e3, s["perf_GHz"]] for s in _summaries(cfg, E1_RUNS.format(q="q6"))
                             if s["label"] == "iostack_worker_off"]
                      + [[_u(s) / 1e3, s["perf_GHz"]] for s in _summaries(cfg, Q6_SAME_PERIOD)],
                      "pool_replica": [pt(c) for c in pool]}
    return nums


def fig_cells(nums: dict, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), gridspec_kw={"width_ratios": [1, 1]})
    for ax, q, xmax in ((axes[0], "q6", 320), (axes[1], "q1", 900)):
        rows = sorted(nums["cells"][q], key=lambda r: -r["measured_s"])
        for i, r in enumerate(rows):
            ax.barh(i, r["measured_s"], height=0.62, color=BLUE if "buffered" in r["cell"] else ORANGE,
                    edgecolor=style.SURFACE, linewidth=2)
            ax.text(xmax * 0.02, i, f"{r['measured_s']:.0f} s", va="center", fontsize=8.5, color="white", fontweight="bold")
            ax.plot(r["v1_s"], i, "o", ms=8, mfc="none", mec=style.INK_2, mew=1.5, zorder=3)
            ax.plot(r["final_s"], i, "D", ms=6, color=style.INK, zorder=4)
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([r["cell"] for r in rows], fontsize=8.5)
        ax.set_xlim(0, xmax)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("time (s), cold, one backend")
        ax.set_title(f"{q.upper()} x SF100")
    fig.legend(handles=[Patch(color=BLUE, label="measured, buffered"), Patch(color=ORANGE, label="measured, direct I/O"),
                        Line2D([], [], ls="", marker="o", mfc="none", mec=style.INK_2, mew=1.5, ms=8, label="first prediction (v1)"),
                        Line2D([], [], ls="", marker="D", color=style.INK, ms=6, label="final model")],
               loc="lower center", ncol=4, bbox_to_anchor=(0.5, -0.06))
    fig.suptitle("The same scan, six I/O paths: measured, first prediction, final model", x=0.01, ha="left",
                 fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0.06, 1, 0.95))
    style.save(fig, out / "fig1_cells.png")


def fig_errors(nums: dict, out: Path) -> None:
    rows = []
    for q in ("q6", "q1"):
        for r in nums["cells"][q]:
            meas = r["measured_s"]
            if q == "q6" and r["cell"] == "worker / buffered":
                meas = st.median(nums["q6_worker_off_same_period_s"])      # see collect()
            rows.append((f"{q.upper()} {r['cell']}", (r["v1_s"] / r["measured_s"] - 1) * 100, (r["final_s"] / meas - 1) * 100))
    held = [(f"{k} (held out)", None, (v["pred_s"] / v["measured_s"] - 1) * 100) for k, v in nums["heldout"].items()]
    rows = rows + held
    fig, ax = plt.subplots(figsize=(7.6, 5.2))
    ax.axvspan(-5, 5, color=style.GRID, alpha=0.6, lw=0)
    ax.axvline(0, color=style.INK_2, lw=0.8)
    for i, (name, v1, final) in enumerate(rows):
        if v1 is not None:
            ax.plot([v1, final], [i, i], color=style.NEUTRAL, lw=1, zorder=1)
            ax.plot(v1, i, "o", ms=8, mfc="none", mec=style.INK_2, mew=1.5, zorder=3)
        ax.plot(final, i, "D", ms=6, color=BLUE if v1 is not None else AQUA, zorder=4)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows], fontsize=8.5)
    ax.invert_yaxis()
    ax.set_xlabel("prediction error (%), predicted / measured - 1   (shaded: ±5 %)")
    ax.grid(axis="y", visible=False)
    ax.set_title("Prediction error per condition: first prediction vs final model")
    ax.legend(handles=[Line2D([], [], ls="", marker="o", mfc="none", mec=style.INK_2, mew=1.5, ms=8, label="first prediction (v1)"),
                       Line2D([], [], ls="", marker="D", color=BLUE, ms=6, label="final model, cells"),
                       Line2D([], [], ls="", marker="D", color=AQUA, ms=6, label="final model, held out (registered before the run)")],
              loc="lower center", bbox_to_anchor=(0.5, -0.3), ncol=1)
    style.save(fig, out / "fig2_errors.png")


def fig_clock(nums: dict, out: Path) -> None:
    c = nums["clock"]
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    ax.plot(*zip(*c["fixed_time"]), "o", color=ORANGE, label="replica, fixed time per read (busy wait)")
    ax.plot(*zip(*c["fixed_cycles"]), "s", color=BLUE, label="replica, fixed cycles per read")
    ax.plot(*zip(*c["pg_q6"]), "D", color=style.INK, ms=7, label="PostgreSQL Q6, sync / direct")
    ax.plot(*zip(*c["pg_scans"]), "^", color=AQUA, ms=7, label="PostgreSQL TPC-H-free scans, sync / direct")
    ax.set_xlim(250, 700)
    ax.set_xlabel("user cycles per 128 KiB read (k)")
    ax.set_ylabel("clock (GHz)")
    ax.set_title("sync / direct: where the clock curve is steepest, fixed time reads the clock high")
    ax.legend(loc="upper left", fontsize=8.5)
    style.save(fig, out / "fig3_clock.png")


def fig_family(nums: dict, out: Path) -> None:
    f = nums["family"]
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    ax.plot(*zip(*f["pool_replica"]), "s", color=ORANGE, label="pool replica (C, PostgreSQL's dispatch), fixed time")
    ax.plot(*zip(*f["pg_scans"]), "^", color=AQUA, ms=7, label="PostgreSQL TPC-H-free scans (min / light / mid)")
    ax.plot(*zip(*f["q6"]), "D", color=style.INK, ms=7, label="PostgreSQL Q6 (11-19 and 11-22)")
    ax.set_xlabel("user cycles per 128 KiB read (k)")
    ax.set_ylabel("clock (GHz)")
    ax.set_ylim(2.6, 3.6)
    ax.set_xlim(200, 1000)
    ax.set_title("worker / buffered: the replica runs 0.3 GHz above PostgreSQL; the scans do not")
    ax.legend(loc="lower right", fontsize=8.5)
    style.save(fig, out / "fig4_family.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None) -> dict:
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg)
    fig_cells(nums, out)
    fig_errors(nums, out)
    fig_clock(nums, out)
    fig_family(nums, out)
    (out / "numbers.json").write_text(json.dumps(nums, indent=1))
    return nums
