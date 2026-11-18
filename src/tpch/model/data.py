"""Measured runs and benchmark results, as observations the model can be calibrated or judged on."""
from __future__ import annotations

import csv
import json
import statistics as st
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from ..config import Config
from . import plan as planmod
from .registry import BENCH, DATASETS


class LeakError(RuntimeError):
    """Calibration tried to read a dataset that is not training data."""


@dataclass
class Run:
    """One measured run (a results/runs/<id>/ directory): whole-run observations and phases.

    Attributes:
        dir: the run directory.
        meta: meta.json (condition, settings, counters).
        summary: summary.json (wall time, perf counts, device totals).
    """
    dir: Path
    meta: dict
    summary: dict

    @property
    def id(self) -> str:
        return self.dir.name

    # --- whole-run observations --------------------------------------------------------------------
    @property
    def wall(self) -> float:
        return self.summary["wall_s"]

    @property
    def cycles(self) -> float:
        return self.summary["perf_cycles"]

    @property
    def user_cycles(self) -> float:
        return self.cycles * self.summary["perf_user_cycles_frac"]

    @property
    def kernel_cycles(self) -> float:
        return self.cycles - self.user_cycles

    @property
    def cpu_s(self) -> float:
        return self.summary["perf_task_clock_s"]

    @property
    def ghz(self) -> float:
        return self.summary["perf_GHz"]

    @property
    def off_cpu(self) -> float:
        return self.wall - self.cpu_s

    @property
    def on_cpu(self) -> float:
        return self.cpu_s / self.wall

    @cached_property
    def plan(self) -> planmod.Node | None:
        f = self.dir / "plan.json"
        return planmod.from_explain(json.loads(f.read_text())) if f.exists() else None

    @cached_property
    def timeline(self) -> list[dict]:
        with open(self.dir / "timeline.csv") as f:
            rows = [{k: float(v or 0) for k, v in r.items()} for r in csv.DictReader(f)]
        return [r for r in rows if r["t_s"] >= 0]

    # --- phases of a spilling hash join ------------------------------------------------------------
    @cached_property
    def phases(self) -> dict:
        """Split at the end of the table scans: the last second whose device read rate is above half
        the median rate of the first 60 % of the run. After it: phase 3 (batches read back)."""
        tl = self.timeline
        head = [r["dev_rMBps"] for r in tl[: max(1, int(len(tl) * 0.6))]]
        thr = 0.5 * st.median(head)
        end = max(i for i, r in enumerate(tl) if r["dev_rMBps"] > thr) + 1
        p12, p3 = tl[:end], tl[end:]
        off = lambda rows: sum(1 - min(r["backend_cpu_pct"], 100) / 100 for r in rows)  # noqa: E731
        burst = [r for r in p12 if r["dev_wMBps"] > 100]
        quiet3 = [r for r in p3 if r["dev_wMBps"] < 1 and r["dev_r_per_s"] > 0]
        req = sum(r["dev_r_per_s"] * r["dev_r_await_ms"] for r in quiet3) / max(1e-9, sum(r["dev_r_per_s"] for r in quiet3))
        return {"p3_start_s": tl[end]["t_s"] if end < len(tl) else None, "p3_len_s": len(p3),
                "off_p12_s": off(p12), "off_p3_s": off(p3), "burst_s": len(burst), "off_burst_s": off(burst),
                "p3_dev_read_MiB": sum(r["dev_rMBps"] for r in p3), "p3_request_ms": req}


    @cached_property
    def phase_clocks(self) -> tuple[float, float] | None:
        """(clock in phases 1-2, clock in phase 3) in GHz, from the per-second cycles (perf -I); None for
        runs without them."""
        tl = self.timeline
        if not tl or "perf_cycles" not in tl[0] or not any(r["perf_task_ms"] for r in tl):
            return None
        split = len(tl) - self.phases["p3_len_s"]

        def clock(rows):
            ms = sum(r["perf_task_ms"] for r in rows)
            return sum(r["perf_cycles"] for r in rows) / (ms * 1e6) if ms else float("nan")
        return clock(tl[:split]), clock(tl[split:])


def _matches(meta: dict, where: dict) -> bool:
    for k, want in {"mode": "plain", **where}.items():
        allowed = want if isinstance(want, tuple) else (want,)
        if meta.get(k) not in allowed:
            return False
    return True


def load(cfg: Config, name: str, purpose: str) -> list[Run]:
    """The runs of a registered dataset (registry.DATASETS).

    Args:
        cfg: configuration (results directory).
        name: dataset name.
        purpose: "calibrate" or "evaluate"; "calibrate" may only read datasets with role "train".

    Returns:
        The runs, in experiment order.

    Raises:
        LeakError: calibration asked for a test or blind dataset.
        LookupError: no run matches the dataset's filter.
    """
    ds = DATASETS[name]
    if purpose == "calibrate" and ds.role != "train":
        raise LeakError(f"calibration may not read dataset {name!r} (role {ds.role})")
    root = cfg.repo / cfg.RESULTS_DIR
    runs = []
    for pattern in ds.experiments:
        for f in sorted((root / "experiments").glob(pattern + ".json")):
            for o in json.loads(f.read_text())["order"]:
                d = root / "runs" / o["run"]
                meta = json.loads((d / "meta.json").read_text())
                if _matches(meta, ds.where):
                    runs.append(Run(d, meta, json.loads((d / "summary.json").read_text())))
    if not runs:
        raise LookupError(f"dataset {name!r} matched no runs")
    return runs


def bench_dirs(cfg: Config, name: str) -> list[Path]:
    """Result directories of the registered benchmark `name` (registry.BENCH)."""
    return [cfg.repo / cfg.RESULTS_DIR / "bench" / d for d in BENCH[name]]


def median(xs) -> float:
    return st.median(list(xs))
