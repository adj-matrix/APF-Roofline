"""Q6 × SF1 (with SF10 in the same experiments): does the SF10 model scale down to SF1?

    ./tpch report q6/sf1 --cond first=scale --cond main=scale3 [--cond repro=scale2]

`first` is the experiment before device-cache eviction existed (its SF1 cold runs hit the RAID
controller cache); `main` is the experiment used for the prediction check; `repro` (optional)
is an earlier experiment of the same design, compared number by number with `main`.
The bandwidth at each table's location comes from the device characterization (hardware/,
`./tpch bench location`): the newest scan made with controller-cache eviction.
Writes fig1_cache.png .. fig3_cycles.png and numbers.json into this directory.
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.bench import location
from tpch.config import Config
from tpch.model.common import HwParams, TableStats
from tpch.report import data, style

from queries.q6.model import predict_q6

TUPLES = {1: 6_001_215, 10: 59_986_052}          # exact count(*) of lineitem
PAGES = {1: 112_555, 10: 1_124_956}               # after VACUUM FULL
FILES = {1: "16384/16632", 10: "16506/16628"}     # relfilenode paths under base/ (location scan)
SCAN = "20261111-184649_location"                 # first location scan with controller-cache eviction
SF_COLOR = {10: style.SERIES[0], 1: style.SERIES[1]}
EARLY_COLOR = style.SERIES[2]                      # SF1 runs before device-cache eviction


def _stats(xs: list[float]) -> dict:
    return data.stats(xs)


def _experiment_runs(cfg: Config, label: str) -> list[dict]:
    return data.experiment_runs(cfg, "q6", label)


def _table_bandwidth(cfg: Config) -> tuple[str, dict[int, float]]:
    """MiB/s at each lineitem's own location, from the location scan this report used (pinned:
    later scans would shift the numbers by < 1 % and break the report's tables)."""
    path = next(p for p in location.scans(cfg) if p.parent.name == SCAN)
    rows = location.load_scan(path)
    return path.parent.name, {sf: location.table_bandwidth(rows, f) for sf, f in FILES.items()}


def _plain(rs: list[dict], sf: int, c: str) -> list[dict]:
    return [r for r in rs if r["mode"] == "plain" and r["sf"] == sf and r["cache"] == c]


def _cpt(r: dict) -> float:  # cycles per tuple
    return r["perf_cycles"] / TUPLES[r["sf"]]


def _cells(runs: list[dict], hw1: HwParams, loc_bw: dict[int, float]) -> tuple[dict, dict]:
    """Per (SF, cache): measurements, model v1/v2 errors. v2's CPU parameter: SF10 cycles/tuple of these runs."""
    cpt10 = {c: st.median(_cpt(r) for r in _plain(runs, 10, c)) for c in ("warm", "cold")}
    cells = {}
    for sf in (1, 10):
        t = TableStats("lineitem", pages=PAGES[sf], tuples=TUPLES[sf])
        for c in ("cold", "warm"):
            rs = _plain(runs, sf, c)
            p1 = predict_q6(t, hw1, c)
            if c == "cold":   # v2: bandwidth measured where this table lies on the disk
                p2 = [predict_q6(t, HwParams(loc_bw[sf], hw1.cpu_ns_per_tuple), "cold").total_s] * len(rs)
            else:             # v2: SF10 cycles/tuple at the clock frequency each run actually got
                p2 = [TUPLES[sf] * cpt10["warm"] / (r["perf_GHz"] * 1e9) for r in rs]
            walls = [r["wall_s"] for r in rs]
            cells[f"sf{sf}_{c}"] = {
                "wall_s": _stats(walls),
                "pred_v1_s": p1.total_s, "pred_v1_range_s": [p1.lower_s, p1.upper_s],
                "pred_v2_s": _stats(p2),
                "err_v1": _stats([w / p1.total_s - 1 for w in walls]),
                "err_v2": _stats([w / p - 1 for w, p in zip(walls, p2)]),
                "dev_MiBps": _stats([r["dev_avg_read_MiBps"] for r in rs]) if c == "cold" else {},
                "dev_r_await_ms": _stats([r.get("dev_avg_r_await_ms") for r in rs]) if c == "cold" else {},
                "GHz": _stats([r["perf_GHz"] for r in rs]),
                "cycles_per_tuple": _stats([_cpt(r) for r in rs]),
                "perf_cpu_s": _stats([r["perf_task_clock_s"] for r in rs]),
                "proc_cpu_s": _stats([r["backend_cpu_s"] for r in rs]),
            }
    return cells, cpt10


def collect(cfg: Config, conds: dict[str, str]) -> dict:
    sf10 = json.loads((cfg.repo / "queries/q6/sf10/numbers.json").read_text())
    hw1 = HwParams(seq_read_MiBps=sf10["fio_seq_buffered_8k_MiBps"]["median"],
                   cpu_ns_per_tuple=sf10["conditions"]["B2"]["cpu_ns_per_tuple"])
    scan, loc_bw = _table_bandwidth(cfg)

    runs = {name: _experiment_runs(cfg, label) for name, label in conds.items()}
    main = runs["main"]
    cells, cpt10 = _cells(main, hw1, loc_bw)
    an = {f"sf{r['sf']}_{r['cache']}": {"exec_s": r["exec_ms"] / 1000, "io_read_s": r["io_read_ms"] / 1000}
          for r in main if r["mode"] != "plain"}
    early_sf1 = _plain(runs["first"], 1, "cold")
    nums = {
        "labels": conds,
        "params_v1": {"seq_MiBps": hw1.seq_read_MiBps, "ns_per_tuple": hw1.cpu_ns_per_tuple},
        "params_v2": {"seq_MiBps_at_table": loc_bw, "location_scan": scan, "cycles_per_tuple_sf10": cpt10},
        "cells": cells,
        "analyze": an,
        "ratio_sf1_sf10": {c: cells[f"sf1_{c}"]["wall_s"]["median"] / cells[f"sf10_{c}"]["wall_s"]["median"]
                           for c in ("cold", "warm")},
        "first_experiment_sf1_cold": {
            "wall_s": _stats([r["wall_s"] for r in early_sf1]),
            "dev_MiBps": _stats([r["dev_avg_read_MiBps"] for r in early_sf1]),
            "dev_r_await_ms": _stats([r.get("dev_avg_r_await_ms") for r in early_sf1]),
        },
        "_runs": runs,
    }
    if "repro" in runs:   # an earlier experiment with the same design: do the numbers reproduce?
        rcells, rcpt10 = _cells(runs["repro"], hw1, loc_bw)
        keys = ("wall_s", "err_v1", "err_v2", "dev_MiBps", "dev_r_await_ms", "GHz", "cycles_per_tuple",
                "perf_cpu_s", "proc_cpu_s")
        nums["repro"] = {
            "cells": {k: {m: {"main": cells[k][m].get("median"), "repro": rcells[k][m].get("median")}
                          for m in keys if cells[k][m]} for k in cells},
            "cycles_per_tuple_sf10": {"main": cpt10, "repro": rcpt10},
        }
    return nums


def fig_cache(nums: dict, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.6))
    groups = [
        ("SF1 cold, experiment 1 (no device-cache eviction)", EARLY_COLOR,
         [r for r in nums["_runs"]["first"] if r["sf"] == 1 and r["cache"] == "cold"]),
        ("SF1 cold, main experiment (cache evicted first)", SF_COLOR[1],
         [r for r in nums["_runs"]["main"] if r["sf"] == 1 and r["cache"] == "cold"]),
        ("SF10 cold, both experiments", SF_COLOR[10],
         [r for k in ("first", "main") for r in nums["_runs"][k] if r["sf"] == 10 and r["cache"] == "cold"]),
    ]
    for name, color, rs in groups:
        ax.plot([r["dev_avg_r_await_ms"] for r in rs], [r["dev_avg_read_MiBps"] for r in rs], "o", color=color,
                markersize=8, markeredgecolor=style.SURFACE, markeredgewidth=1.5, label=name, linestyle="none")
    ax.set_xscale("log")
    ax.set_xlim(0.08, 2.5)
    ax.set_ylim(0, 600)
    ax.axvspan(0.08, 0.4, color=style.GRID, alpha=0.5, linewidth=0)
    ax.text(0.09, 40, "served by the RAID\ncontroller cache", fontsize=8.5, color=style.INK_2, va="bottom")
    ax.text(1.15, 40, "served by\nthe disks", fontsize=8.5, color=style.INK_2, va="bottom", ha="center")
    ax.set_xlabel("average device read latency per request (ms, log scale)   [/proc/diskstats: read_ms / reads]")
    ax.set_ylabel("device read rate during the query (MiB/s)")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=1, borderaxespad=0.2)
    ax.set_title("drop_caches does not make SF1 cold: the RAID controller still held the file", pad=62)
    style.save(fig, out / "fig1_cache.png")


def fig_prediction(nums: dict, out: Path) -> None:
    rows = [("sf1_cold", "SF1 cold"), ("sf10_cold", "SF10 cold"), ("sf1_warm", "SF1 warm"),
            ("sf10_warm", "SF10 warm\n(v2 calibrated here)")]
    fig, ax = plt.subplots(figsize=(8, 3.8))
    for y, (k, _) in enumerate(rows):
        for dy, key, color, label in ((-0.14, "err_v1", style.NEUTRAL, "model v1: fio bandwidth, ns/tuple from SF10"),
                                      (0.14, "err_v2", style.INK, "model v2: bandwidth at the table's location, "
                                                                  "cycles/tuple from SF10 at the run's clock")):
            e = nums["cells"][k][key]
            ax.hlines(y + dy, e["min"] * 100, e["max"] * 100, color=color, linewidth=1.5)
            ax.plot(e["median"] * 100, y + dy, "o", color=color, markersize=8, markeredgecolor=style.SURFACE,
                    markeredgewidth=1.5, label=label if y == 0 else None)
            ax.text(max(e["max"] * 100, e["median"] * 100) + 1.2, y + dy, f"{e['median']:+.1%}", va="center",
                    fontsize=8.5, color=style.INK)
    ax.axvline(0, color=style.INK_2, linewidth=0.8)
    ax.set_yticks(range(len(rows)), [r[1] for r in rows])
    ax.set_ylim(len(rows) - 0.5, -0.85)          # inverted; room above the first row for the label
    ax.text(0.6, -0.62, "0 = measured equals predicted   (right: slower than predicted)",
            fontsize=8.5, color=style.INK_2, va="center")
    hi = max(nums["cells"][k][e]["max"] for k, _ in rows for e in ("err_v1", "err_v2")) * 100
    ax.set_xlim(-10, hi + 12)
    ax.set_xlabel("measured / predicted − 1 (%)   dot = median of 5 runs, line = min–max")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=1, borderaxespad=0.2)
    ax.set_title("SF10 parameters predict SF1 once location and clock are accounted for", pad=48)
    style.save(fig, out / "fig2_prediction.png")


def fig_cycles(nums: dict, out: Path) -> None:
    main = [r for r in nums["_runs"]["main"] if r["mode"] == "plain"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4.2), gridspec_kw={"width_ratios": [1, 1.25]})
    for x, (sf, c) in enumerate([(1, "warm"), (10, "warm"), (1, "cold"), (10, "cold")]):
        rs = [r for r in main if r["sf"] == sf and r["cache"] == c]
        ys = [r["perf_cycles"] / TUPLES[sf] for r in rs]
        xs = [x + (i - 2) * 0.06 for i in range(len(ys))]
        a1.plot(xs, ys, "o", color=SF_COLOR[sf], markersize=8, markeredgecolor=style.SURFACE, markeredgewidth=1.5,
                linestyle="none", label=f"SF{sf}" if c == "warm" else None)
    a1.set_xticks(range(4), ["SF1\nwarm", "SF10\nwarm", "SF1\ncold", "SF10\ncold"])
    a1.set_ylim(0, 1000)
    a1.set_ylabel("backend CPU cycles per lineitem tuple")
    a1.set_title("Cycles per tuple do not depend on SF")
    a1.legend(loc="lower right")

    cpt = nums["params_v2"]["cycles_per_tuple_sf10"]["warm"]
    every = [r for rs in nums["_runs"].values() for r in rs if r["mode"] == "plain" and r["cache"] == "warm"]
    for sf in (1, 10):
        rs = [r for r in every if r["sf"] == sf]
        a2.plot([r["perf_GHz"] for r in rs], [r["wall_s"] / TUPLES[sf] * 1e9 for r in rs], "o", color=SF_COLOR[sf],
                markersize=8, markeredgecolor=style.SURFACE, markeredgewidth=1.5, linestyle="none", label=f"SF{sf}")
    g = [2.1 + i * 0.05 for i in range(30)]
    a2.plot(g, [cpt / x for x in g], color=style.INK_2, linewidth=1, linestyle="--")
    a2.text(2.42, cpt / 2.42 + 4, f"{cpt:.0f} cycles / clock", fontsize=8, color=style.INK_2)
    a2.set_xlabel("average clock while on CPU (GHz)   [perf: cycles / task-clock]")
    a2.set_ylabel("warm wall time per tuple (ns)")
    a2.set_title(f"Warm-run spread is the clock frequency ({len(every)} runs, 3 experiments)")
    a2.legend(loc="upper right")
    fig.tight_layout(w_pad=3)
    style.save(fig, out / "fig3_cycles.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None) -> dict:
    assert {"first", "main"} <= set(conds) <= {"first", "main", "repro"}, \
        "use --cond first=<label> --cond main=<label> [--cond repro=<label>]"
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg, conds)
    fig_cache(nums, out)
    fig_prediction(nums, out)
    fig_cycles(nums, out)
    public = {k: v for k, v in nums.items() if not k.startswith("_")}
    (out / "numbers.json").write_text(json.dumps(public, indent=1, default=str))
    return public
