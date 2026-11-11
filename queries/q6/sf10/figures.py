"""Report #1 (Q6 @ SF10): collect runs per condition, predict, compare, draw figures.

    ./tpch report q6/sf10 --cond A=v1 --cond B1=v2defrag --cond B2=condB --cond C=condC --timeline A B2 C

Writes (by default into this directory) fig1_fio.png .. fig4_breakdown.png and numbers.json (every number the
report text cites, so text and figures come from the same data).
"""
from __future__ import annotations

import csv
import json
import statistics as st
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.config import Config
from tpch.model.common import HwParams, TableStats
from tpch.report import style

from queries.q6.model import predict_q6

LINEITEM_TUPLES_SF10 = 59_986_052   # exact count(*) of tpch_sf10.lineitem
# Human-readable condition descriptions by run label (fallback: the label itself).
DESCRIPTIONS = {
    "v1": "fragmented, schedutil",
    "v2defrag": "defragmented, schedutil (period 1)",
    "condB": "defragmented, schedutil (period 2)",
    "condC": "defragmented, performance",
}


def _desc(name: str, label: str) -> str:
    return f"{name}: {DESCRIPTIONS.get(label, label)}"


def _runs(cfg: Config, label: str, cache: str, mode: str) -> list[dict]:
    out = []
    for d in sorted((cfg.repo / cfg.RESULTS_DIR / "runs").glob(f"*_q6_sf10_{mode}_{cache}_{label}_r*")):
        if (d / "summary.json").exists():
            s = json.loads((d / "summary.json").read_text())
            s["dir"] = d
            out.append(s)
    return out


def _stats(xs: list[float]) -> dict:
    return {"median": st.median(xs), "min": min(xs), "max": max(xs), "n": len(xs), "values": xs}


def _fio(cfg: Config) -> dict[str, list[dict]]:
    by_test: dict[str, list[dict]] = {}
    for f in sorted((cfg.repo / cfg.RESULTS_DIR / "bench").glob("*_io/io.csv")):
        for r in csv.DictReader(open(f)):
            by_test.setdefault(r["test"], []).append(r)
    return by_test


def collect(cfg: Config, conds: dict[str, str]) -> dict:
    fio = _fio(cfg)
    seq_bw = _stats([float(r["MiBps"]) for r in fio["seq_buffered_8k_t1"]])
    nums: dict = {"fio_seq_buffered_8k_MiBps": seq_bw, "conditions": {}}
    for name, label in conds.items():
        cold = _runs(cfg, label, "cold", "plain")
        warm = _runs(cfg, label, "warm", "plain")
        an_cold = _runs(cfg, label, "cold", "analyze")
        an_warm = _runs(cfg, label, "warm", "analyze")
        pages = an_cold[0]["shared_read_blks"] if an_cold else None
        c = {
            "label": label,
            "cold_wall_s": _stats([r["wall_s"] for r in cold]),
            "warm_wall_s": _stats([r["wall_s"] for r in warm]),
            "cold_dev_MiBps": _stats([r["dev_avg_read_MiBps"] for r in cold]),
            "cold_rareq_kB": _stats([r["dev_avg_rareq_kB"] for r in cold]),
            "cold_busy": _stats([r["dev_busy_frac"] for r in cold]),
            "cold_usr_s": _stats([r["backend_usr_s"] for r in cold]),
            "cold_sys_s": _stats([r["backend_sys_s"] for r in cold]),
            "warm_usr_s": _stats([r["backend_usr_s"] for r in warm]),
            "warm_sys_s": _stats([r["backend_sys_s"] for r in warm]),
            "totals_source": sorted({r["totals_source"] for r in cold + warm}),
            "pages": pages,
            "governor": json.loads((cold[0]["dir"] / "meta.json").read_text())["sys"]["governor"]
            if cold else None,
        }
        for tag, runs in (("analyze_cold", an_cold), ("analyze_warm", an_warm)):
            if runs:
                r = runs[0]
                c[tag] = {"exec_s": r["exec_ms"] / 1000, "io_read_s": r["io_read_ms"] / 1000,
                          "scan_self_s": r["plan_nodes"][1]["self_ms"] / 1000,
                          "agg_self_s": r["plan_nodes"][0]["self_ms"] / 1000,
                          "rows_out": r["plan_nodes"][1]["actual_rows"]}
        # Model: disk from the benchmark (same for every condition), CPU calibrated on this
        # condition's warm runs.
        ns_per_tuple = c["warm_wall_s"]["median"] * 1e9 / LINEITEM_TUPLES_SF10
        hw = HwParams(seq_bw["median"], ns_per_tuple)
        p = predict_q6(TableStats("lineitem", pages, LINEITEM_TUPLES_SF10), hw)
        c["cpu_ns_per_tuple"] = ns_per_tuple
        c["prediction"] = p.as_dict()
        c["error_vs_lower"] = c["cold_wall_s"]["median"] / p.lower_s - 1
        c["overlap_frac"] = (p.upper_s - c["cold_wall_s"]["median"]) / p.components["cpu_s"]
        nums["conditions"][name] = c
    return nums


# ---------------------------------------------------------------------------------- figures

def fig_fio(cfg: Config, out: Path) -> None:
    fio = _fio(cfg)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.4), gridspec_kw={"wspace": 0.3})
    sizes = [4, 8, 16, 32, 64, 128, 256, 512, 1024]
    for ax, key, title, ylabel in (
        (a1, "MiBps", "Sequential read bandwidth", "MiB/s"),
        (a2, "dev_rareq_kB", "Request size reaching the device", "kB per request"),
    ):
        for mode, color in (("direct", style.SERIES[0]), ("buffered", style.SERIES[1])):
            xs, med, lo, hi = [], [], [], []
            for kb in sizes:
                name = f"seq_{mode}_{kb // 1024}m_t1" if kb >= 1024 else f"seq_{mode}_{kb}k_t1"
                vals = [float(r[key]) for r in fio.get(name, []) if r[key]]
                if vals:
                    xs.append(kb); med.append(st.median(vals)); lo.append(min(vals)); hi.append(max(vals))
            if not xs:
                continue
            ax.plot(xs, med, marker="o", color=color, label="O_DIRECT" if mode == "direct" else "page cache")
            ax.vlines(xs, lo, hi, color=color, linewidth=1.2)
        ax.set_xscale("log", base=2)
        ax.set_xticks(sizes, [f"{k}K" if k < 1024 else "1M" for k in sizes])
        ax.set_xlabel("application read size")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.set_ylim(bottom=0)
    a1.legend(loc="lower right")
    a1.annotate("8K reads through the page cache\nalready reach the plateau",
                xy=(8, st.median(float(r["MiBps"]) for r in fio["seq_buffered_8k_t1"])),
                xytext=(11, 120), fontsize=8, color=style.INK_2,
                arrowprops={"arrowstyle": "-", "color": style.INK_2, "linewidth": 0.8})
    a2.axhline(256, color=style.INK_2, linewidth=0.8, linestyle="--")
    a2.text(4.2, 262, "max_sectors_kb = 256", fontsize=8, color=style.INK_2)
    style.save(fig, out / "fig1_fio.png")


def fig_timeline(cfg: Config, nums: dict, out: Path, conds: list[str]) -> None:
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 4.8), sharex=True, gridspec_kw={"hspace": 0.25})
    for i, name in enumerate(conds):
        c = nums["conditions"][name]
        runs = _runs(cfg, c["label"], "cold", "plain")
        med = c["cold_wall_s"]["median"]
        run = min(runs, key=lambda r: abs(r["wall_s"] - med))["dir"]   # the median run
        rows = list(csv.DictReader(open(run / "timeline.csv")))
        t = [float(r["t_s"]) for r in rows]
        num = lambda col: [float(r[col]) if r[col] else 0.0 for r in rows]  # noqa: E731
        color = style.SERIES[i]
        a1.plot(t, num("dev_rMBps"), color=color, label=_desc(name, c["label"]), linewidth=1.6)
        a2.plot(t, num("backend_cpu_pct"), color=color, linewidth=1.6)
    a1.set_ylabel("device read MiB/s")
    a1.set_ylim(0, 230)
    a2.set_ylabel("backend CPU %")
    a2.set_ylim(0, 100)
    a2.set_xlabel("seconds since query start")
    a1.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=len(conds), borderaxespad=0.2, fontsize=8.5)
    a1.set_title("Cold Q6 at SF10: the disk is the bottleneck for the whole scan", pad=22)
    style.save(fig, out / "fig2_timeline.png")


def fig_prediction(nums: dict, out: Path) -> None:
    names = list(nums["conditions"])
    fig, ax = plt.subplots(figsize=(9, 0.9 + 0.75 * len(names)))
    for y, name in enumerate(names):
        c = nums["conditions"][name]
        p = c["prediction"]
        ax.barh(y, p["upper_s"] - p["lower_s"], left=p["lower_s"], height=0.38,
                color=style.SERIES[0], alpha=0.25, edgecolor=style.SERIES[0], linewidth=1)
        w = c["cold_wall_s"]
        ax.hlines(y, w["min"], w["max"], color=style.SERIES[1], linewidth=1.5)
        ax.plot(w["median"], y, "o", color=style.SERIES[1], markersize=8,
                markeredgecolor=style.SURFACE, markeredgewidth=2)
        ax.text(p["upper_s"] + 0.4, y, f"measured {w['median']:.1f} s  ({c['error_vs_lower']:+.0%} vs IO-only)",
                va="center", fontsize=9, color=style.INK)
    ax.set_yticks(range(len(names)), [_desc(n, nums["conditions"][n]["label"]) for n in names])
    ax.invert_yaxis()
    ax.set_xlim(0, max(nums["conditions"][n]["prediction"]["upper_s"] for n in names) + 18)
    ax.set_xlabel("cold-cache Q6 time (s)")
    ax.set_title("Cold-cache Q6: predicted range vs measured", pad=20)
    ax.text(0, 1.02, "band = model range [max(IO, CPU), IO + CPU];  dot = measured median, line = min-max",
            transform=ax.transAxes, fontsize=8, color=style.INK_2, va="bottom")
    style.save(fig, out / "fig3_prediction.png")


def fig_breakdown(nums: dict, out: Path) -> None:
    names = list(nums["conditions"])
    labels, usr, sys_, wait = [], [], [], []
    for name in names:
        c = nums["conditions"][name]
        for cache in ("cold", "warm"):
            wall = c[f"{cache}_wall_s"]["median"]
            u, s = c[f"{cache}_usr_s"]["median"], c[f"{cache}_sys_s"]["median"]
            labels.append(f"{name} {cache}")
            usr.append(u); sys_.append(s); wait.append(max(wall - u - s, 0))
    fig, ax = plt.subplots(figsize=(9, 0.8 + 0.42 * len(labels)))
    y = range(len(labels))
    gap = 0.15  # surface gap between segments (seconds, visual only)
    ax.barh(y, usr, height=0.55, color=style.SERIES[0], label="backend user CPU")
    ax.barh(y, [max(s - gap, 0) for s in sys_], left=[u + gap for u in usr], height=0.55,
            color=style.SERIES[1], label="backend kernel CPU")
    ax.barh(y, [max(w - gap, 0) for w in wait], left=[u + s + gap for u, s in zip(usr, sys_)],
            height=0.55, color=style.NEUTRAL, label="not on CPU (waiting for I/O)")
    for i, (u, s, w) in enumerate(zip(usr, sys_, wait)):
        ax.text(u + s + w + 0.4, i, f"{u + s + w:.1f} s  (CPU {u:.1f} + {s:.1f})", va="center", fontsize=8.5)
    ax.set_yticks(list(y), labels)
    ax.invert_yaxis()
    ax.set_xlim(0, max(u + s + w for u, s, w in zip(usr, sys_, wait)) + 16)
    ax.set_xlabel("seconds (median of 3 runs)")
    ax.set_title("Where the backend's time goes", pad=24)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, borderaxespad=0.2)
    style.save(fig, out / "fig4_breakdown.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None) -> dict:
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg, conds)
    fig_fio(cfg, out)
    fig_timeline(cfg, nums, out, (timeline or list(conds))[:3])
    fig_prediction(nums, out)
    fig_breakdown(nums, out)

    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items() if k != "dir"}
        if isinstance(o, list):
            return [clean(v) for v in o]
        return o
    (out / "numbers.json").write_text(json.dumps(clean(nums), indent=2, default=str))
    return nums
