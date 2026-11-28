"""Q1 × SF1 and SF10 (same experiments): CPU cost from its pieces, and the CPU-bound cold run.

    queries/q1/sf10/report.sh        (the results it reads are listed there)

`micro` = the piece microbenchmarks (queries/q1/micro.sql, SF1 warm), `main` = the experiment.
Writes fig1_pieces.png .. fig4_queries.png and numbers.json into this directory.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.bench import location
from tpch.config import Config
from tpch.report import style
from tpch.report.data import experiment_runs, need, stats

LINEITEM = {1: 6_001_215, 10: 59_986_052}
MIB = {1: 112_555 * 8192 / 2**20, 10: 1_124_956 * 8192 / 2**20}
FILES = {1: "16384/16632", 10: "16506/16628"}
KERNEL_COLD = 210.0                    # cycles/tuple, cold - warm, Q6 (docs/3-q1-prediction.md A3)
SF_COLOR = {10: style.SERIES[0], 1: style.SERIES[1]}
# Q1's pieces: (label, statement, base statement, how many times Q1 contains it)
PIECES = [("scan, count(*)", "m_scan", None, 1), ("read columns + filter", "m_filter", "m_scan", 1),
          ("group by 2 columns", "m_group", "m_filter", 1), ("sum(quantity)", "m_sum_qty", "m_group", 1),
          ("sum(price)", "m_sum_price", "m_group", 1), ("sum(price×(1−disc))", "m_disc_price", "m_group", 1),
          ("sum(price×(1−disc)×(1+tax))", "m_charge", "m_group", 1), ("avg(discount)", "m_avg_disc", "m_group", 1),
          ("avg after its sum (×2)", "m_sum_avg_qty", "m_sum_qty", 2)]


def _cpt(r: dict) -> float:
    return r["perf_cycles"] / LINEITEM[r["sf"]]


def collect(cfg: Config, conds: dict[str, str], scan: str) -> dict:
    micro = experiment_runs(cfg, conds["micro"].split(","))
    main = [r for r in experiment_runs(cfg, conds["main"].split(",")) if r["query"] == "q1"]
    med = {q.split(".")[1]: stats(_cpt(r) for r in micro if r["query"] == q)["median"]
           for q in {r["query"] for r in micro}}
    pieces = [{"piece": lab, "cycles_per_tuple": n * (med[s] - (med[b] if b else 0))} for lab, s, b, n in PIECES]
    pred_cpt = sum(p["cycles_per_tuple"] for p in pieces)
    noshare_cpt = pred_cpt - 2 * (med["m_sum_avg_qty"] - med["m_sum_qty"]) + 2 * (med["m_avg_qty"] - med["m_group"])
    rows = location.load_scan(next(p for p in location.scans(cfg) if p.parent.name == scan))
    bw = {sf: location.table_bandwidth(rows, f) for sf, f in FILES.items()}

    cells = {}
    for sf in (1, 10):
        io = MIB[sf] / bw[sf]
        for c in ("cold", "warm"):
            rs = [r for r in main if r["sf"] == sf and r["cache"] == c and r["mode"] == "plain"]
            cpt = pred_cpt + (KERNEL_COLD if c == "cold" else 0)
            cpu = [cpt * LINEITEM[sf] / (r["perf_GHz"] * 1e9) for r in rs]
            pred = [max(io, x) if c == "cold" else x for x in cpu]
            walls = [r["wall_s"] for r in rs]
            cell = {"wall_s": stats(walls), "GHz": stats(r["perf_GHz"] for r in rs),
                    "cycles_per_tuple": stats(_cpt(r) for r in rs), "pred_cycles_per_tuple": cpt,
                    "pred_s": stats(pred), "err": stats(p / w - 1 for w, p in zip(walls, pred)),
                    "err_cycles": stats(cpt / _cpt(r) - 1 for r in rs),
                    "perf_cpu_s": stats(r["perf_task_clock_s"] for r in rs),
                    "cpu_util": stats(r["perf_task_clock_s"] / r["wall_s"] for r in rs),
                    "ipc": stats(r["perf_ipc"] for r in rs)}
            if c == "cold":
                cell.update(pred_io_s=io, pred_upper_s=stats(io + x for x in cpu),
                            dev_MiBps=stats(r["dev_avg_read_MiBps"] for r in rs),
                            dev_busy_frac=stats(r.get("dev_busy_frac") for r in rs),
                            dev_r_await_ms=stats(r.get("dev_avg_r_await_ms") for r in rs))
            cells[f"sf{sf}_{c}"] = cell
    analyze = {f"sf{r['sf']}_{r['cache']}": {"wall_s": r["wall_s"], "run": r["run"]}
               for r in main if r["mode"] == "analyze"}
    return {"labels": conds, "location_scan": scan, "bandwidth_MiBps": bw,
            "micro_cycles_per_tuple": med, "pieces": pieces, "pred_cycles_per_tuple": pred_cpt,
            "pred_cycles_per_tuple_without_sharing": noshare_cpt, "cells": cells, "analyze": analyze,
            "_main": main}


def _dot(ax, x, y, color, label=None):
    ax.plot(x, y, "o", color=color, markersize=8, markeredgecolor=style.SURFACE, markeredgewidth=1.5,
            linestyle="none", label=label)


def fig_pieces(nums: dict, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    left = 0.0
    n = len(nums["pieces"])
    for y, p in enumerate(nums["pieces"]):
        v = p["cycles_per_tuple"]
        ax.barh(y, v, left=left, color=style.SERIES[0] if v > 0 else style.NEUTRAL, height=0.6)
        ax.text(left + max(v, 0) + 30, y, f"{v:+.0f}" if y else f"{v:.0f}", va="center", fontsize=8.5)
        left += v
    ax.barh(n, nums["pred_cycles_per_tuple"], color=style.INK, height=0.6)
    ax.text(nums["pred_cycles_per_tuple"] + 30, n, f"{nums['pred_cycles_per_tuple']:.0f} predicted", va="center",
            fontsize=8.5)
    for sf in (1, 10):
        c = nums["cells"][f"sf{sf}_warm"]["cycles_per_tuple"]
        _dot(ax, c["median"], n + 1 + (sf == 10), SF_COLOR[sf])
        ax.text(c["max"] + 30, n + 1 + (sf == 10), f"{c['median']:.0f} measured", va="center", fontsize=8.5)
    ax.set_yticks(range(n + 3), [p["piece"] for p in nums["pieces"]] + ["Q1 = sum of pieces", "Q1 SF1 warm",
                                                                       "Q1 SF10 warm"])
    ax.invert_yaxis()
    ax.set_xlim(0, 5200)
    ax.set_xlabel("backend CPU cycles per lineitem tuple (pieces: SF1 warm microbenchmarks)")
    ax.set_title("Q1's pieces add up to 8 % more than Q1 (avg shares its state with sum)")
    style.save(fig, out / "fig1_pieces.png")


def fig_prediction(nums: dict, out: Path) -> None:
    c = nums["cells"]
    rows = [("SF1 warm", c["sf1_warm"]["err"]), ("SF10 warm", c["sf10_warm"]["err"]),
            ("SF1 cold", c["sf1_cold"]["err"]), ("SF10 cold", c["sf10_cold"]["err"])]
    fig, ax = plt.subplots(figsize=(8, 3.4))
    for y, (_, e) in enumerate(rows):
        ax.hlines(y, e["min"] * 100, e["max"] * 100, color=style.INK, linewidth=1.5)
        _dot(ax, e["median"] * 100, y, style.INK)
        ax.text(max(e["max"], e["median"]) * 100 + 0.6, y, f"{e['median']:+.1%}", va="center", fontsize=8.5)
    ax.axvline(0, color=style.INK_2, linewidth=0.8)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows])
    ax.set_ylim(len(rows) - 0.5, -1.0)
    ax.text(0.3, -0.75, "0 = predicted equals measured   (right: predicted too slow)", fontsize=8.5,
            color=style.INK_2, va="center")
    lo = min(r[1]["min"] for r in rows) * 100
    hi = max(r[1]["max"] for r in rows) * 100
    ax.set_xlim(min(-5, lo - 3), max(9, hi + 6))
    ax.set_xlabel("predicted / measured − 1 (%)\nprediction at each run's clock; dot = median, line = min–max")
    ax.set_title("Pieces measured at SF1 predict Q1 at SF1 and SF10, warm and cold", pad=12)
    style.save(fig, out / "fig2_prediction.png")


def fig_cold(cfg: Config, nums: dict, out: Path) -> None:
    run = next(r for r in nums["_main"] if r["sf"] == 10 and r["cache"] == "cold" and r["mode"] == "plain")
    rows = list(csv.DictReader(open(cfg.repo / cfg.RESULTS_DIR / "runs" / run["run"] / "timeline.csv")))
    t = [float(r["t_s"]) for r in rows]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 4.6), sharex=True)
    a1.plot(t, [float(r["backend_cpu_pct"]) for r in rows], color=style.SERIES[0])
    a1.set_ylabel("backend CPU %")
    a1.set_ylim(0, 110)
    a2.plot(t, [float(r["dev_rMBps"]) * 1e6 / 2**20 for r in rows], color=style.SERIES[1])
    a2.axhline(nums["bandwidth_MiBps"][10], color=style.INK_2, linewidth=0.8, linestyle="--")
    a2.text(1, nums["bandwidth_MiBps"][10] + 6, f"disk at this location: {nums['bandwidth_MiBps'][10]:.0f} MiB/s",
            fontsize=8, color=style.INK_2)
    a2.set_ylabel("device read MiB/s")
    a2.set_ylim(0, 230)
    a2.set_xlabel("time since query start (s)")
    a1.set_title(f"Q1 × SF10 cold ({run['wall_s']:.1f} s): the backend is on CPU all the time, the disk is not")
    style.save(fig, out / "fig3_cold.png")


def fig_queries(cfg: Config, nums: dict, out: Path, sets: dict[str, tuple[str, list[str]]]) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.2))
    x = 0
    ticks = []
    labelled = set()
    for name, (q, ids) in sets.items():
        rs = [r for r in experiment_runs(cfg, ids) if r.get("query", q) == q and r["mode"] == "plain"]
        for c in ("warm", "cold"):
            for sf in (1, 10):
                ys = [_cpt(r) for r in rs if r["sf"] == sf and r["cache"] == c]
                xs = [x + (i - 2) * 0.05 for i in range(len(ys))]
                _dot(ax, xs, ys, SF_COLOR[sf], label=None if sf in labelled else f"SF{sf}")
                labelled.add(sf)
                ticks.append((x, f"{name}\n{c}"))
                x += 0.35 if sf == 1 else 0.65
    ax.set_xticks([t[0] + 0.17 for t in ticks[::2]], [t[1] for t in ticks[::2]])
    ax.set_ylim(0, None)
    ax.set_ylabel("backend CPU cycles per lineitem tuple")
    ax.legend(loc="upper left")
    ax.set_title("Per-tuple CPU cost: Q6 and Q1 are linear in SF, Q3 warm is not")
    style.save(fig, out / "fig4_queries.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None,
          inputs: dict[str, list[str]] | None = None) -> dict:
    assert {"micro", "main"} == set(conds), "use --cond micro=<experiments> --cond main=<experiments>"
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    (scan,), q6_ids, q3_ids = need(inputs, "scan", "q6", "q3")
    nums = collect(cfg, conds, scan)
    fig_pieces(nums, out)
    fig_prediction(nums, out)
    fig_cold(cfg, nums, out)
    fig_queries(cfg, nums, out, {"Q6": ("q6", q6_ids), "Q3": ("q3", q3_ids), "Q1": ("q1", conds["main"].split(","))})
    public = {k: v for k, v in nums.items() if not k.startswith("_")}
    (out / "numbers.json").write_text(json.dumps(public, indent=1, default=str))
    return public
