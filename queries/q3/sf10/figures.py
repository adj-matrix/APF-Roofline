"""Q3 × SF1 and SF10 (same experiments): predicted vs measured, and where the non-linearity is.

    queries/q3/sf10/report.sh        (the results it reads are listed there)

`cal` = the SF1 warm calibration runs (before the prediction's part B); `main` = the experiment.
Device bandwidth and memory latency come from hardware/ (--input scan=, mem=).
Writes fig1_prediction.png .. fig4_wait.png and numbers.json into this directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.bench import location, mem
from tpch.config import Config
from tpch.parse.plan import parse_plan
from tpch.report import style
from tpch.report.data import experiment_runs, need, stats

from queries.q3.model import HashTable, io_seconds, probe_ns, probe_ns_split, warm_seconds_scaled

SF_COLOR = {10: style.SERIES[0], 1: style.SERIES[1]}
PAGES = {(1, "customer"): 3600, (1, "orders"): 26136, (1, "lineitem"): 112555,
         (10, "customer"): 35932, (10, "orders"): 261196, (10, "lineitem"): 1124956}
FILES = {(1, "customer"): "16384/16426", (1, "orders"): "16384/16437", (1, "lineitem"): "16384/16632",
         (10, "customer"): "16506/16548", (10, "orders"): "16506/16559", (10, "lineitem"): "16506/16628"}
LINEITEM = {1: 6_001_215, 10: 59_986_052}
OUTER = {1: HashTable(147_126, 12), 10: HashTable(1_461_923, 12)}      # orders ⋈ customer
INNER = {1: HashTable(30_142, 4), 10: HashTable(300_276, 4)}           # customer (BUILDING)
OUTER_PROBES = {1: 3_241_776, 10: 32_334_250}                          # lineitem after filter
INNER_PROBES = {1: 727_305, 10: 7_289_442}                             # orders after filter
SCALE = 10.0
# EXPLAIN node -> phase (pre-order ids of the plan shape in model.py)
PHASES = {"customer scan + hash": ("Seq Scan:customer", "Hash:1"), "orders scan + join + hash": (
    "Seq Scan:orders", "Hash Join:1", "Hash:0"), "lineitem scan": ("Seq Scan:lineitem",),
    "probe outer hash": ("Hash Join:0",), "aggregate + sort": ("Aggregate", "Sort", "Limit")}


def _bandwidth(cfg: Config, scan: str) -> dict:
    path = next(p for p in location.scans(cfg) if p.parent.name == scan)
    rows = location.load_scan(path)
    return {k: location.table_bandwidth(rows, f) for k, f in FILES.items()}


def _waits(curve: dict, sf: int) -> float:
    """Seconds spent waiting for both hash tables at their sizes (probes do not overlap)."""
    return (probe_ns(OUTER_PROBES[sf], OUTER[sf], mem.latency_at(curve, OUTER[sf].bytes))
            + probe_ns(INNER_PROBES[sf], INNER[sf], mem.latency_at(curve, INNER[sf].bytes))) * 1e-9


def _waits_v2(curve: dict, sf: int) -> float:
    """v2 (after measuring): bucket array and tuples as two working sets."""
    lat = lambda size: mem.latency_at(curve, size)  # noqa: E731
    return (probe_ns_split(OUTER_PROBES[sf], OUTER[sf], lat) + probe_ns_split(INNER_PROBES[sf], INNER[sf], lat)) * 1e-9


def _v2_misses(curve: dict, sf: int) -> float:
    """Expected DRAM accesses of the probes under v2: a load misses with probability
    (L(size) - L(LLC)) / (L(DRAM) - L(LLC)), from the same curve."""
    l3, dram = curve[1 << 23], curve[1 << 30]
    frac = lambda size: min(1.0, max(0.0, (mem.latency_at(curve, size) - l3) / (dram - l3)))  # noqa: E731
    return sum(p * (frac(h.bucket_bytes) + h.alpha * frac(h.tuple_bytes))
               for p, h in ((OUTER_PROBES[sf], OUTER[sf]), (INNER_PROBES[sf], INNER[sf])))


def _sel(rs: list[dict], sf: int, cache: str, mode: str = "plain") -> list[dict]:
    return [r for r in rs if r["sf"] == sf and r["cache"] == cache and r["mode"] == mode]


def _phases(cfg: Config, run: str) -> dict[str, dict]:
    plan = parse_plan(json.loads((cfg.repo / cfg.RESULTS_DIR / "runs" / run / "plan.json").read_text()))
    seen: dict[str, int] = {}
    out = {p: {"self_s": 0.0, "io_read_s": 0.0} for p in PHASES}
    for n in plan["nodes"]:
        key = f"{n['type']}:{n['relation']}" if n["relation"] else n["type"]
        if n["type"] in ("Hash", "Hash Join"):
            key = f"{n['type']}:{seen.setdefault(n['type'], 0)}"
            seen[n["type"]] += 1
        phase = next(p for p, keys in PHASES.items() if key in keys)
        out[phase]["self_s"] += n["self_ms"] / 1000
        out[phase]["io_read_s"] += n["self_io_read_ms"] / 1000
    return out


def collect(cfg: Config, conds: dict[str, str], scan: str, mem_run: str) -> dict:
    cal = experiment_runs(cfg, conds["cal"].split(","))
    main = experiment_runs(cfg, conds["main"].split(","))
    bw = _bandwidth(cfg, scan)
    curve = mem.latency_curve(cfg.repo / cfg.RESULTS_DIR / "bench" / mem_run / "mem.csv")
    cal_warm = _sel(cal, 1, "warm")
    cal_cyc = stats(r["perf_cycles"] for r in cal_warm)["median"]
    cal_ghz = stats(r["perf_GHz"] for r in cal_warm)["median"]
    w1 = _waits(curve["local"], 1)
    w10 = {p: _waits(curve[p], 10) for p in ("local", "remote")}
    w1_v2, w10_v2 = _waits_v2(curve["local"], 1), _waits_v2(curve["local"], 10)

    cells = {}
    for sf in (1, 10):
        io = io_seconds({t: (PAGES[(sf, t)] * 8192 / 2**20, bw[(sf, t)]) for t in ("customer", "orders", "lineitem")})
        for c in ("cold", "warm"):
            rs = _sel(main, sf, c)
            walls = [r["wall_s"] for r in rs]
            cell = {
                "wall_s": stats(walls), "GHz": stats(r["perf_GHz"] for r in rs),
                "cycles": stats(r["perf_cycles"] for r in rs),
                "cycles_per_lineitem_tuple": stats(r["perf_cycles"] / LINEITEM[sf] for r in rs),
                "llc_load_misses": stats(r["perf_llc_load_misses"] for r in rs),
                "perf_cpu_s": stats(r["perf_task_clock_s"] for r in rs),
                "ipc": stats(r["perf_ipc"] for r in rs),
            }
            if c == "cold":
                pred = sum(io.values())
                cell.update(pred_io_by_table_s=io, pred_s=pred, err=stats(pred / w - 1 for w in walls),
                            dev_MiBps=stats(r["dev_avg_read_MiBps"] for r in rs),
                            dev_r_await_ms=stats(r.get("dev_avg_r_await_ms") for r in rs),
                            dev_read_MiB=stats(r["dev_read_MiB"] for r in rs))
            elif sf == 1:     # calibration check: cal cycles at each run's clock
                p = [cal_cyc / (r["perf_GHz"] * 1e9) for r in rs]
                cell.update(pred_s=stats(p), err=stats(q / w - 1 for w, q in zip(walls, p)))
            else:
                for name, small, wait in (("linear", None, None), ("local", w1, w10["local"]),
                                          ("remote", w1, w10["remote"]), ("v2", w1_v2, w10_v2)):
                    if wait is None:
                        p = [SCALE * cal_cyc / (r["perf_GHz"] * 1e9) for r in rs]
                    else:
                        p = [warm_seconds_scaled(cal_cyc, cal_ghz, small, SCALE, r["perf_GHz"], wait * 1e9) for r in rs]
                    cell[f"pred_{name}_s"] = stats(p)
                    cell[f"err_{name}"] = stats(q / w - 1 for w, q in zip(walls, p))
            cells[f"sf{sf}_{c}"] = cell

    # Measured hash-table wait at SF10 warm: what is left after the linearly scaled compute part.
    w10_runs = _sel(main, 10, "warm")
    s1 = _sel(main, 1, "warm")
    cyc1 = stats(r["perf_cycles"] for r in s1)["median"]
    ghz1 = stats(r["perf_GHz"] for r in s1)["median"]
    compute1 = cyc1 - w1 * ghz1 * 1e9
    waits = [r["wall_s"] - SCALE * compute1 / (r["perf_GHz"] * 1e9) for r in w10_runs]
    loads = OUTER_PROBES[10] * OUTER[10].loads_per_probe() + INNER_PROBES[10] * INNER[10].loads_per_probe()
    miss1 = stats(r["perf_llc_load_misses"] for r in s1)["median"]
    extra_miss = [r["perf_llc_load_misses"] - SCALE * miss1 for r in w10_runs]
    nonlinear = {
        "sf1_warm_cycles_main": cyc1, "sf1_warm_GHz_main": ghz1,
        "measured_wait_s": stats(waits), "predicted_wait_s": w10, "v2_wait_s": w10_v2,
        "ns_per_extra_llc_miss": stats(w / m * 1e9 for w, m in zip(waits, extra_miss)),
        "v2_extra_misses_per_outer_probe": (_v2_misses(curve["local"], 10) - SCALE * _v2_misses(curve["local"], 1))
                                           / OUTER_PROBES[10],
        "hash_loads_sf10": loads,
        "ns_per_hash_load": stats(w / loads * 1e9 for w in waits),
        "extra_llc_misses": stats(extra_miss),
        "extra_llc_misses_per_outer_probe": stats(m / OUTER_PROBES[10] for m in extra_miss),
        "predicted_loads_per_outer_probe": OUTER[10].loads_per_probe(),
        "latency_at_outer_hash_ns": {p: mem.latency_at(curve[p], OUTER[10].bytes) for p in curve},
    }
    analyze = {f"sf{r['sf']}_{r['cache']}": {"wall_s": r["wall_s"], "phases": _phases(cfg, r["run"])}
               for r in main if r["mode"] == "analyze"}
    hash_actual = {}
    for r in main:
        if r["mode"] == "analyze" and r["cache"] == "warm":
            plan = json.loads((cfg.repo / cfg.RESULTS_DIR / "runs" / r["run"] / "plan.json").read_text())
            top = plan[0] if isinstance(plan, list) else plan
            found = []

            def walk(n):
                if n["Node Type"] in ("Hash", "Aggregate"):
                    found.append({k: n.get(k) for k in ("Node Type", "Actual Rows", "Plan Rows", "Hash Buckets",
                                                         "Hash Batches", "Peak Memory Usage", "HashAgg Batches",
                                                         "Disk Usage")})
                for ch in n.get("Plans", []):
                    walk(ch)
            walk(top["Plan"])
            hash_actual[f"sf{r['sf']}"] = found
    return {
        "labels": conds, "location_scan": scan, "mem_run": mem_run,
        "bandwidth_MiBps": {f"sf{k[0]}_{k[1]}": v for k, v in bw.items()},
        "calibration": {"cycles": cal_cyc, "GHz": cal_ghz, "n": len(cal_warm), "wait_s_sf1": w1},
        "hash_tables_predicted": {f"sf{sf}": {"outer_MiB": OUTER[sf].bytes / 2**20, "inner_MiB": INNER[sf].bytes / 2**20}
                                  for sf in (1, 10)},
        "hash_tables_measured": hash_actual,
        "cells": cells, "nonlinear": nonlinear, "analyze": analyze,
        "_main": main, "_curve": curve,
    }


def _dot(ax, x, y, color, label=None):
    ax.plot(x, y, "o", color=color, markersize=8, markeredgecolor=style.SURFACE, markeredgewidth=1.5,
            linestyle="none", label=label)


def fig_prediction(nums: dict, out: Path) -> None:
    c = nums["cells"]
    rows = [("SF1 cold (I/O sum)", c["sf1_cold"]["err"], style.INK),
            ("SF10 cold (I/O sum)", c["sf10_cold"]["err"], style.INK),
            ("SF1 warm (calibration)", c["sf1_warm"]["err"], style.NEUTRAL),
            ("SF10 warm: linear from SF1", c["sf10_warm"]["err_linear"], style.NEUTRAL),
            ("SF10 warm: + DRAM wait, local", c["sf10_warm"]["err_local"], style.INK),
            ("SF10 warm: + DRAM wait, remote", c["sf10_warm"]["err_remote"], style.INK),
            ("SF10 warm: v2, buckets ≠ tuples", c["sf10_warm"]["err_v2"], style.SERIES[0])]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for y, (_, e, color) in enumerate(rows):
        ax.hlines(y, e["min"] * 100, e["max"] * 100, color=color, linewidth=1.5)
        _dot(ax, e["median"] * 100, y, color)
        ax.text(max(e["max"], e["median"]) * 100 + 1.2, y, f"{e['median']:+.1%}", va="center", fontsize=8.5)
    ax.axvline(0, color=style.INK_2, linewidth=0.8)
    ax.set_yticks(range(len(rows)), [r[0] for r in rows])
    ax.set_ylim(len(rows) - 0.5, -1.0)
    ax.text(0.6, -0.75, "0 = predicted equals measured   (right: predicted too slow)", fontsize=8.5,
            color=style.INK_2, va="center")
    lo = min(r[1]["min"] for r in rows) * 100
    hi = max(r[1]["max"] for r in rows) * 100
    ax.set_xlim(min(-10, lo - 5), hi + 12)
    ax.set_xlabel("predicted / measured − 1 (%)   dot = median of 5 runs, line = min–max")
    ax.set_title("Q3: cold is the sum of three scans; SF10 warm needs the DRAM wait", pad=12)
    style.save(fig, out / "fig1_prediction.png")


def fig_cycles(nums: dict, q6: dict, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.2))
    main = [r for r in nums["_main"] if r["mode"] == "plain" and r["cache"] == "warm"]
    for x, (q, sf) in enumerate([("Q6", 1), ("Q6", 10), ("Q3", 1), ("Q3", 10)]):
        if q == "Q6":
            ys = q6[sf]
        else:
            ys = [r["perf_cycles"] / LINEITEM[sf] for r in main if r["sf"] == sf]
        xs = [x + (i - (len(ys) - 1) / 2) * 0.06 for i in range(len(ys))]
        _dot(ax, xs, ys, SF_COLOR[sf], label=f"SF{sf}" if x < 2 else None)
    nl = nums["nonlinear"]
    ghz10 = nums["cells"]["sf10_warm"]["GHz"]["median"]
    lin = SCALE * nums["calibration"]["cycles"] / LINEITEM[10]
    compute = SCALE * (nums["calibration"]["cycles"] - nums["calibration"]["wait_s_sf1"]
                       * nums["calibration"]["GHz"] * 1e9)
    for label, wait, color in (("v1, hash table in remote DRAM", nl["predicted_wait_s"]["remote"], style.INK_2),
                               ("v1, hash table in local DRAM", nl["predicted_wait_s"]["local"], style.INK_2),
                               ("v2, buckets and tuples apart", nl["v2_wait_s"], style.SERIES[0])):
        y = (compute + wait * ghz10 * 1e9) / LINEITEM[10]
        ax.hlines(y, 2.75, 3.25, color=color, linewidth=1, linestyle="--")
        ax.text(3.3, y, label, fontsize=8, color=style.INK_2, va="center")
    ax.hlines(lin, 2.75, 3.25, color=style.NEUTRAL, linewidth=1, linestyle="--")
    ax.text(3.3, lin, "linear from SF1", fontsize=8, color=style.INK_2, va="center")
    ax.set_xticks(range(4), ["Q6\nSF1", "Q6\nSF10", "Q3\nSF1", "Q3\nSF10"])
    ax.set_xlim(-0.5, 4.6)
    ax.set_ylim(0, None)
    ax.set_ylabel("backend CPU cycles per lineitem tuple (warm)")
    ax.legend(loc="upper left")
    ax.set_title("Q6 costs the same per tuple at SF1 and SF10; Q3 does not")
    style.save(fig, out / "fig2_cycles.png")


def fig_breakdown(nums: dict, out: Path) -> None:
    keys = [k for k in ("sf1_warm", "sf1_cold", "sf10_warm", "sf10_cold") if k in nums["analyze"]]
    colors = dict(zip(PHASES, [style.SERIES[1], style.SERIES[2], style.NEUTRAL, style.SERIES[0], style.INK_2]))
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), gridspec_kw={"width_ratios": [1, 1]})
    for ax, sf in zip(axes, (1, 10)):
        ks = [k for k in keys if k.startswith(f"sf{sf}_")]
        for y, k in enumerate(ks):
            left = 0.0
            for p in PHASES:
                v = nums["analyze"][k]["phases"][p]["self_s"]
                ax.barh(y, v, left=left, color=colors[p], edgecolor=style.SURFACE, linewidth=2, height=0.6,
                        label=p if (sf == 1 and y == 0) else None)
                left += v
        ax.set_yticks(range(len(ks)), [k.split("_")[1] for k in ks])
        ax.invert_yaxis()
        ax.set_xlabel("EXPLAIN ANALYZE time, exclusive per node (s)")
        ax.set_title(f"SF{sf}")
    fig.legend(loc="lower left", bbox_to_anchor=(0.06, 0.96), ncol=5, frameon=False, fontsize=8.5)
    fig.tight_layout(w_pad=3)
    style.save(fig, out / "fig3_breakdown.png")


def fig_wait(nums: dict, out: Path) -> None:
    nl = nums["nonlinear"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.4), gridspec_kw={"width_ratios": [1.3, 1]})
    rows = [("v1, local DRAM", nl["predicted_wait_s"]["local"], style.INK),
            ("v1, remote DRAM", nl["predicted_wait_s"]["remote"], style.INK),
            ("v2, buckets ≠ tuples (local)", nl["v2_wait_s"], style.SERIES[0])]
    for y, (name, v, color) in enumerate(rows):
        _dot(a1, v, y, color)
        a1.text(v + 0.25, y, f"{v:.1f} s", va="center", fontsize=8.5)
    m = nl["measured_wait_s"]
    y = len(rows)
    a1.hlines(y, m["min"], m["max"], color=style.SERIES[1], linewidth=1.5)
    _dot(a1, m["median"], y, style.SERIES[1])
    a1.text(m["max"] + 0.25, y, f"{m['median']:.1f} s", va="center", fontsize=8.5)
    a1.set_yticks(range(len(rows) + 1), [r[0] for r in rows] + ["measured (5 runs)"])
    a1.invert_yaxis()
    a1.set_xlim(0, 11)
    a1.set_xlabel("SF10 warm: time waiting for the hash tables (s)")
    a1.set_title("Predicted vs measured DRAM wait")
    names = ["v1: every load misses", "v2: from the latency curve", "measured: LLC-load-misses"]
    vals = [nl["predicted_loads_per_outer_probe"], nl["v2_extra_misses_per_outer_probe"],
            nl["extra_llc_misses_per_outer_probe"]["median"]]
    for y, (n, v, color) in enumerate(zip(names, vals, (style.INK, style.SERIES[0], style.SERIES[1]))):
        _dot(a2, v, y, color)
        a2.text(v + 0.04, y, f"{v:.2f}", va="center", fontsize=8.5)
    a2.set_yticks(range(3), names)
    a2.invert_yaxis()
    a2.set_xlim(0, 2.1)
    a2.set_xlabel("extra DRAM loads per probe (SF10 − 10×SF1)")
    a2.set_title(f"{nl['ns_per_extra_llc_miss']['median']:.0f} ns per extra miss: local DRAM")
    fig.tight_layout(w_pad=3)
    style.save(fig, out / "fig4_wait.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None,
          inputs: dict[str, list[str]] | None = None) -> dict:
    assert {"cal", "main"} == set(conds), "use --cond cal=<experiments> --cond main=<experiments>"
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    (scan,), (mem_run,), q6_ids = need(inputs, "scan", "mem", "q6")
    nums = collect(cfg, conds, scan, mem_run)
    q6 = {sf: [r["perf_cycles"] / LINEITEM[sf] for r in experiment_runs(cfg, q6_ids)
               if r["sf"] == sf and r["cache"] == "warm" and r["mode"] == "plain"] for sf in (1, 10)}
    nums["q6_warm_cycles_per_tuple"] = {sf: stats(v) for sf, v in q6.items()}
    fig_prediction(nums, out)
    fig_cycles(nums, q6, out)
    fig_breakdown(nums, out)
    fig_wait(nums, out)
    public = {k: v for k, v in nums.items() if not k.startswith("_")}
    (out / "numbers.json").write_text(json.dumps(public, indent=1, default=str))
    return public
