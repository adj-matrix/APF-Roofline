"""Figures for the clock microbenchmarks (docs/7 Parts A-D, docs/8 M1) and the clock of long runs.

    hardware/report.sh        (the results it reads are listed there)

Reads the --input clock benches (cells.jsonl) and long runs, the query runs of the registered datasets,
model/params.json (the fitted curves) and the turbostat log of Q1 x SF1000.
Writes into hardware/:
  fig3_clock_vs_oncpu.png   clock vs the worker's on-CPU share, by idle length; queries and fitted curves on top
  fig4_clock_vs_idle.png    clock vs the length of each idle, one line per on-CPU share
  fig5_clock_causes.png     what lowers the clock of a task that never sleeps: other cores (yes), memory (no), scans
  fig6_long_run_clock.png   Q1 x SF1000 vs SF100: intervals in which the backend changed core run slower
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib.pyplot as plt  # noqa: E402

from tpch.config import load as load_cfg  # noqa: E402
from tpch.model import data  # noqa: E402
from tpch.report import style  # noqa: E402
from tpch.report.data import need  # noqa: E402

OUT = ROOT / "hardware"
BLUE, ORANGE, AQUA = style.SERIES
RAMP = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]       # ordinal blue ramp, steps 250 / 400 / 550 / 700
SHORT_MS, LONG_MS = 3.5, 8.0                               # idle classes (M1: the step lies between 3 and ~9 ms)


def cells(benches: list[str]) -> list[dict]:
    out = []
    for bench in benches:
        for line in (ROOT / "results/bench" / bench / "cells.jsonl").read_text().splitlines():
            r = json.loads(line)
            r["run"] = bench
            if r["load"].startswith("sleep:") and "idle_ms_mean" not in r:
                r["idle_ms_mean"] = 0.3 + 0.07                 # docs/7 cells: 0.3 ms sleeps (+ timer slack)
            out.append(r)
    return out


def idle_class(r: dict) -> str | None:
    if r["load"] in ("spin", "spin@pin"):
        return "none"
    if r["load"].startswith(("sleep:", "dread:")) and "@" not in r["load"]:
        return "short" if r["idle_ms_mean"] < SHORT_MS else "long" if r["idle_ms_mean"] > LONG_MS else None
    return None


def queries(cfg) -> list[tuple[str, float, float]]:
    out = []
    for ds, label in (("q6_sf100", "Q6 SF100"), ("q1_sf100", "Q1 SF100"), ("q6_sf1000", "Q6 SF1000"),
                      ("q1_sf1000", "Q1 SF1000")):
        runs = data.load(cfg, ds, "evaluate")
        out.append((label, st.median(r.summary["backend_cpu_util"] for r in runs),
                    st.median(r.summary["perf_GHz"] for r in runs)))
    return out


def fitted_curves() -> dict:
    p = {d["name"]: d["value"] for d in json.loads((ROOT / "model/params.json").read_text())["params"]}
    return {"scan phase": p["clock_GHz_vs_on_cpu"], "read-back phase": p["clock_readback_GHz_vs_on_cpu"]}


def fig_oncpu(cs: list[dict], qs, curves, out: Path) -> None:
    fig, (ax, zx) = plt.subplots(1, 2, figsize=(9.6, 4.6), gridspec_kw={"width_ratios": [1.6, 1]})
    for a in (ax, zx):
        for cls, color, label in (("short", BLUE, f"idle < {SHORT_MS:g} ms each (sleep)"),
                                  ("long", ORANGE, f"idle > {LONG_MS:g} ms each (sleep or device read)"),
                                  ("none", AQUA, "never idle (spin)")):
            pts = [(r["on_cpu"], r["GHz"]) for r in cs if idle_class(r) == cls]
            a.scatter(*zip(*pts), s=26, color=color, edgecolor=style.SURFACE, linewidth=1, label=label, zorder=3)
        for name, ls in zip(curves, ("--", ":")):
            xs, ys = zip(*curves[name])
            a.plot(xs, ys, ls, color=style.INK_2, linewidth=1.5, zorder=2,
                   label=f"fitted on Q3 runs ({name})" if a is ax else None)
    offsets = {"Q1 SF100": (-8, 0), "Q1 SF1000": (-10, 16), "Q6 SF100": (-12, 0), "Q6 SF1000": (-10, -18)}
    for label, d, f in qs:
        zx.scatter([d], [f], marker="D", s=34, color=style.INK, edgecolor=style.SURFACE, linewidth=1, zorder=4)
        zx.annotate(label, (d, f), xytext=offsets.get(label, (-8, 0)), textcoords="offset points", ha="right",
                    va="center", fontsize=8, color=style.INK,
                    bbox={"boxstyle": "round,pad=0.15", "facecolor": style.SURFACE, "edgecolor": "none", "alpha": 0.85},
                    arrowprops={"arrowstyle": "-", "color": style.INK_2, "linewidth": 0.6, "shrinkA": 0, "shrinkB": 3})
    ax.set_xlabel("worker's share of time on CPU")
    ax.set_ylabel("clock while on CPU (GHz)")
    ax.set_ylim(1.0, 3.65)
    ax.set_xlim(0.48, 1.01)
    ax.axvspan(0.935, 1.003, color=style.GRID, alpha=0.5, zorder=0)
    h, l = ax.get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=3, fontsize=8)
    zx.set_xlim(0.935, 1.003)
    zx.set_ylim(3.0, 3.56)
    zx.set_xlabel("on-CPU share (zoom: where Q6 / Q1 run)")
    zx.set_title("queries (diamonds) vs microbenchmark", fontsize=9, fontweight="normal")
    fig.suptitle("At the same on-CPU share, many short idles cost far more clock than a few long ones",
                 x=0.01, ha="left", fontsize=11, fontweight="bold")
    style.save(fig, out / "fig3_clock_vs_oncpu.png")


def fig_idle(cs: list[dict], out: Path) -> None:
    by = defaultdict(list)
    for r in cs:
        if r["run"].endswith(("clock_m1", "clock_m1b")) and r["load"].startswith("sleep:") and r["load"].count(":") == 2:
            by[float(r["load"].split(":")[1])].append(r)
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    ds = sorted(d for d in by if d in (0.6, 0.75, 0.9, 0.97))
    dy = {0.9: 9, 0.97: 0, 0.75: -9}
    for d, color in zip(ds, RAMP):
        g = defaultdict(list)
        for r in by[d]:
            g[round(r["idle_ms_mean"], 1)].append(r["GHz"])
        xs = sorted(g)
        ax.plot(xs, [st.median(g[x]) for x in xs], "-o", color=color, markersize=5,
                markeredgecolor=style.SURFACE, markeredgewidth=1)
        for x in xs:
            ax.scatter([x] * len(g[x]), g[x], s=8, color=color, alpha=0.5, zorder=2)
        ax.annotate(f"{d:.0%} on CPU", (xs[-1], st.median(g[xs[-1]])), xytext=(6, dy.get(d, 0)), textcoords="offset points",
                    va="center", fontsize=8, color=style.INK_2)
    dr = [r for r in cs if r["load"].startswith("dread:")]
    ax.scatter([r["idle_ms_mean"] for r in dr], [r["GHz"] for r in dr], marker="s", s=34, facecolor="none",
               edgecolor=ORANGE, linewidth=1.5, zorder=4, label="idle = synchronous device read (60 / 75 / 90 %)")
    ax.axvspan(SHORT_MS, LONG_MS, color=style.GRID, alpha=0.6, zorder=0)
    ax.text((SHORT_MS * LONG_MS) ** 0.5, 3.6, "the step lies\nin here", ha="center", va="top", fontsize=8,
            color=style.INK_2)
    ax.set_xscale("log")
    ax.set_xticks([0.1, 0.3, 1, 3, 10])
    ax.set_xticklabels(["0.1", "0.3", "1", "3", "10"])
    ax.set_xlim(0.1, 40)
    ax.set_xlabel("length of each idle (ms, log scale)")
    ax.set_ylabel("clock while on CPU (GHz)")
    ax.set_title("The clock depends on how long each idle lasts, not on what the task waits for")
    ax.set_ylim(1.0, 3.65)
    ax.legend(loc="center left", bbox_to_anchor=(0.0, 0.33))
    style.save(fig, out / "fig4_clock_vs_idle.png")


def fig_causes(cs: list[dict], out: Path) -> None:
    groups = [("reference", [("spin", "busy loop, nothing else")]),
              ("other cores awake", [("spin+wakers:1", "+ 1 core waking every 1 ms"),
                                     ("spin+wakers:3", "+ 3 cores"), ("spin+wakers:7", "+ 7 cores")]),
              ("memory stalls", [("mem:copy", "256 MiB copy"), ("mem:gather", "random 8 B loads, 2 GiB")]),
              ("query stand-ins", [("scan:1.0", "scan, 1.0 ms CPU / 128 KiB (Q1)"),
                                   ("scan:0.2", "scan, 0.2 ms CPU / 128 KiB (Q6)"),
                                   ("scan:0.25:16", "scan + temp writes (Q3 phases 1-2)")])]
    rows = []
    for g, items in groups:
        for load, label in items:
            v = [r["GHz"] for r in cs if r["load"] == load]
            if v:
                rows.append((g, label, v))
    spin = st.median(rows[0][2])
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    y = list(range(len(rows)))[::-1]
    colors = {"reference": AQUA, "other cores awake": BLUE, "memory stalls": ORANGE, "query stand-ins": style.INK_2}
    for yi, (g, label, v) in zip(y, rows):
        ax.scatter(v, [yi] * len(v), s=14, color=colors[g], alpha=0.45, zorder=2, linewidth=0)
        ax.scatter([st.median(v)], [yi], s=60, color=colors[g], edgecolor=style.SURFACE, linewidth=1.5, zorder=3)
        ax.text(st.median(v), yi + 0.32, f"{st.median(v):.2f}", ha="center", fontsize=8, color=style.INK_2)
    ax.axvline(spin, color=style.INK_2, linewidth=0.8, linestyle="--", zorder=1)
    ax.set_yticks(y)
    ax.set_yticklabels([r[1] for r in rows], fontsize=8)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(3.0, 3.56)
    ax.set_ylim(-0.6, len(rows) - 0.3)
    ax.set_xlabel("clock while on CPU (GHz); large dot = median, small dots = cells; dashed = busy loop")
    ax.set_title("A task that never sleeps loses clock when other cores wake; memory stalls do not matter")
    handles = [plt.Line2D([], [], marker="o", linestyle="", color=c, markersize=7) for c in colors.values()]
    ax.legend(handles, colors.keys(), loc="upper center", bbox_to_anchor=(0.45, -0.13), ncol=4)
    style.save(fig, out / "fig5_clock_causes.png")


def _turbostat_intervals(path: Path):
    lines = [l.rstrip("\n").split("\t") for l in path.read_text().splitlines()]
    hdr = lines[0]
    ix = {h: i for i, h in enumerate(hdr)}
    blocks, cur = [], []
    for l in lines[1:]:
        if l == hdr:
            blocks.append(cur)
            cur = []
        elif len(l) >= 4:
            cur.append(l)
    blocks.append(cur)
    for b in blocks:
        cpus = [l for l in b if l[ix["CPU"]] != "-"]
        if not cpus:
            continue
        hot = [l for l in cpus if float(l[ix["Busy%"]]) > 15]
        if not hot:
            continue
        w = sum(float(l[ix["Busy%"]]) for l in hot)
        mhz = sum(float(l[ix["Busy%"]]) * float(l[ix["Bzy_MHz"]]) for l in hot) / w
        stayed = max(float(l[ix["Busy%"]]) for l in cpus) >= 90
        yield float(b[0][ix["Time_Of_Day_Seconds"]]), mhz, stayed


def fig_long_run(runs: dict[str, str], turbostat: Path, out: Path) -> None:
    spans = {}
    for k, d in runs.items():
        meta = json.loads((ROOT / "results/runs" / d / "meta.json").read_text())
        s = json.loads((ROOT / "results/runs" / d / "summary.json").read_text())
        t0 = time.mktime(time.strptime(d[:15], "%Y%m%d-%H%M%S"))
        spans[k] = (t0 + 10, t0 + s["wall_s"] - 10, s["perf_GHz"])
    iv = list(_turbostat_intervals(turbostat))
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.6), sharey=True, gridspec_kw={"width_ratios": [2, 1]})
    for ax, (k, (lo, hi, perf)) in zip(axes, spans.items()):
        sel = [x for x in iv if lo <= x[0] < hi]
        stay = [m / 1000 for _, m, s in sel if s]
        mig = [m / 1000 for _, m, s in sel if not s]
        share = len(mig) / len(sel)
        bars = [("stayed on\none core", stay, AQUA), ("changed\ncore", mig, BLUE)]
        for i, (lab, v, c) in enumerate(bars):
            ax.bar(i, st.mean(v) - 3.0, bottom=3.0, width=0.6, color=c)
            ax.text(i, st.mean(v) + 0.01, f"{st.mean(v):.2f}\n{len(v)} intervals", ha="center", va="bottom",
                    fontsize=8, color=style.INK_2)
        ax.set_xticks([0, 1])
        ax.set_xticklabels([b[0] for b in bars], fontsize=8)
        ax.set_title(f"Q1 {k}: {share:.0%} of 10-s intervals changed core\n(perf: {perf:.2f} GHz on average)",
                     fontsize=9, fontweight="normal")
        ax.set_ylim(3.0, 3.6)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("busy-weighted clock (GHz, turbostat)")
    fig.suptitle("Long runs change core more often, and those intervals run ~3 % slower", x=0.02, ha="left",
                 fontsize=11, fontweight="bold", y=1.04)
    style.save(fig, out / "fig6_long_run_clock.png")


def main() -> None:
    ap = argparse.ArgumentParser(description="clock figures")
    ap.add_argument("--input", action="append", default=[], metavar="NAME=VALUE[,VALUE...]")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    inputs: dict[str, list[str]] = {}
    for item in a.input:
        name, value = item.split("=", 1)
        inputs.setdefault(name, []).extend(value.split(","))
    benches, (turbostat,), (sf1000,), (sf100,) = need(inputs, "cells", "turbostat", "long_sf1000", "long_sf100")
    style.apply()
    cfg = load_cfg()
    a.out.mkdir(parents=True, exist_ok=True)
    cs = cells(benches)
    fig_oncpu(cs, queries(cfg), fitted_curves(), a.out)
    fig_idle(cs, a.out)
    fig_causes(cs, a.out)
    fig_long_run({"SF1000": sf1000, "SF100": sf100}, ROOT / turbostat, a.out)
    print("figures ->", a.out)


if __name__ == "__main__":
    main()
