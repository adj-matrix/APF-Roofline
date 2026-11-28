"""E3 (attribution): where the user cycles go, Q6 / Q1 x SF100, cold, sync / buffered (docs/ext/attribution.md).

    queries/ext/attribution/report.sh        (the results it reads are listed there)

fig1_breakdown   user cycles per 128 KiB read, by component group, as workers are added (A1)
fig2_scaling     the buffer manager's share and the cycles per read relative to one backend, vs workers (A1)
fig3_floor       the per-page components on five workloads, three of them held out (A2)
A1 used the first component rules and kept only the top 40 functions: ~2 % of the cycles it files under
"other" belong to the buffer manager / expressions (docs, "What A1 settles"). A2 uses the corrected rules.
Writes fig1..fig3 and numbers.json into this directory. The runs and benches are given as --input.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import matplotlib.pyplot as plt

from tpch.config import Config
from tpch.report import style
from tpch.report.data import need

BLUE, ORANGE, AQUA = style.SERIES
GROUPS = [("per-page floor: verification, buffer manager, heap page", ("checksum", "bufmgr", "heap_page"), BLUE),
          ("tuple work: deforming, expressions", ("deform", "expression"), ORANGE),
          ("aggregation, executor, other", ("agg_exec", "other", "libc", "other_dso", "hash_bytes"), AQUA)]
WORKLOADS = [("q6", "Q6", 53.3), ("q1", "Q1", 53.3), ("scan.lineitem_min", "lineitem_min", 53.3),
             ("scan.orders_light", "orders", 57.4), ("scan.customer_light", "customer", 41.7)]


def _summary(cfg: Config, pattern: str) -> dict:
    (d,) = glob.glob(str(cfg.repo / cfg.RESULTS_DIR / "runs" / pattern))
    return json.loads((Path(d) / "summary.json").read_text())


def _per_read(profile: dict, reads: float, period: int, keys) -> float:
    """k cycles per 128 KiB read of the components `keys`."""
    c = profile["components"]
    return sum(c.get(k, {}).get("cycles_share", 0) for k in keys) * profile["samples_cycles"] * period / reads / 1e3


def collect(cfg: Config, inputs: dict[str, list[str]] | None) -> dict:
    (a1,), (a1_period,), (a1_runs,), (a2,), (a2_period,), (a2_runs,) = need(
        inputs, "a1", "a1_period", "a1_runs", "a2", "a2_period", "a2_runs")
    a1_period, a2_period = int(a1_period), int(a2_period)
    bench = cfg.repo / cfg.RESULTS_DIR / "bench"
    nums: dict = {"a1": {}, "a2": {}}
    for q in ("q6", "q1"):
        for n in (0, 1, 3, 7):
            prof = json.loads((bench / a1 / f"{q}_n{n}.json").read_text())
            reads = _summary(cfg, a1_runs.format(q=q, n=n))["dev_read_MiB"] / 0.125
            nums["a1"][f"{q}_n{n}"] = {
                "groups_kcycles_per_read": {g: _per_read(prof, reads, a1_period, keys) for g, keys, _ in GROUPS},
                "bufmgr_share": prof["components"]["bufmgr"]["cycles_share"],
                "kcycles_per_read": prof["samples_cycles"] * a1_period / reads / 1e3,
                "kinstr_per_read": prof["samples_instr"] * a1_period / reads / 1e3}
    runs = {s["query"]: s for s in (json.loads((Path(d) / "summary.json").read_text())
                                    for d in glob.glob(str(cfg.repo / cfg.RESULTS_DIR / "runs" / a2_runs)))}
    for q, name, tpp in WORKLOADS:
        prof = json.loads((bench / a2 / f"{q}.json").read_text())
        reads = runs[q]["dev_read_MiB"] / 0.125
        nums["a2"][name] = {"tuples_per_page": tpp, **{k: _per_read(prof, reads, a2_period, (k,))
                                                       for k in ("checksum", "bufmgr", "heap_page")}}
    return nums


def fig_breakdown(nums: dict, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.6))
    for ax, q in zip(axes, ("q6", "q1")):
        bottom = [0.0] * 4
        for g, _, color in GROUPS:
            ys = [nums["a1"][f"{q}_n{n}"]["groups_kcycles_per_read"][g] for n in (0, 1, 3, 7)]
            ax.bar(range(4), ys, bottom=bottom, width=0.6, color=color, edgecolor=style.SURFACE, linewidth=2,
                   label=g if q == "q6" else None)
            bottom = [b + y for b, y in zip(bottom, ys)]
        ax.set_xticks(range(4))
        ax.set_xticklabels([f"n = {n}" for n in (0, 1, 3, 7)])
        ax.set_ylabel("k user cycles per 128 KiB read")
        ax.grid(axis="x", visible=False)
        ax.set_title(f"{q.upper()} x SF100, sync / buffered")
    fig.legend(loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.06), fontsize=8.5)
    fig.suptitle("Where the user cycles go, as parallel workers are added", x=0.01, ha="left", fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0.06, 1, 0.94))
    style.save(fig, out / "fig1_breakdown.png")


def fig_scaling(nums: dict, out: Path) -> None:
    fig, (a, b) = plt.subplots(1, 2, figsize=(9.6, 3.4))
    for q, color, marker in (("q6", BLUE, "o"), ("q1", ORANGE, "s")):
        rows = [nums["a1"][f"{q}_n{n}"] for n in (0, 1, 3, 7)]
        a.plot([0, 1, 3, 7], [r["bufmgr_share"] * 100 for r in rows], "-" + marker, color=color, label=q.upper())
        b.plot([0, 1, 3, 7], [r["kcycles_per_read"] / rows[0]["kcycles_per_read"] for r in rows], "-" + marker, color=color,
               label=f"{q.upper()} cycles")
        b.plot([0, 1, 3, 7], [r["kinstr_per_read"] / rows[0]["kinstr_per_read"] for r in rows], ":" + marker, color=color,
               mfc=style.SURFACE, label=f"{q.upper()} instructions")
    a.set_ylim(0, 12)
    a.set_ylabel("buffer manager, % of user cycles")
    a.set_title("The buffer manager stays small and flat", fontsize=10)
    a.legend(loc="upper left", fontsize=8.5)
    b.set_ylim(0.9, 1.15)
    b.axhline(1.0, color=style.INK_2, lw=0.8)
    b.set_ylabel("per read, relative to n = 0")
    b.set_title("Q1's cycles per read do not grow; Q6's do while it waits", fontsize=10)
    b.legend(loc="upper left", fontsize=8, ncol=2)
    for ax in (a, b):
        ax.set_xticks([0, 1, 3, 7])
        ax.set_xlabel("parallel workers n")
    fig.tight_layout()
    style.save(fig, out / "fig2_scaling.png")


def fig_floor(nums: dict, out: Path) -> None:
    comps = [("checksum", "page verification", BLUE), ("bufmgr", "buffer manager", ORANGE),
             ("heap_page", "heap page scan (per tuple)", AQUA)]
    names = [n for _, n, _ in WORKLOADS]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    w = 0.26
    for j, (key, label, color) in enumerate(comps):
        ax.bar([i + (j - 1) * w for i in range(len(names))], [nums["a2"][n][key] for n in names], width=w, color=color,
               edgecolor=style.SURFACE, linewidth=2, label=label)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([f"{n}\n{nums['a2'][n]['tuples_per_page']:.0f} / page" + ("\nheld out" if i >= 2 else "")
                        for i, n in enumerate(names)], fontsize=8.5)
    ax.set_ylabel("k cycles per 128 KiB read")
    ax.grid(axis="x", visible=False)
    ax.set_title("A query-independent floor under the per-tuple cost")
    ax.legend(loc="upper center", ncol=3, bbox_to_anchor=(0.5, -0.32))
    style.save(fig, out / "fig3_floor.png")


def build(cfg: Config, conds: dict[str, str], out: Path, timeline: list[str] | None = None,
          inputs: dict[str, list[str]] | None = None) -> dict:
    style.apply()
    out.mkdir(parents=True, exist_ok=True)
    nums = collect(cfg, inputs)
    fig_breakdown(nums, out)
    fig_scaling(nums, out)
    fig_floor(nums, out)
    (out / "numbers.json").write_text(json.dumps(nums, indent=1))
    return nums
