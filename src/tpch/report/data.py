"""Loading measured runs for figures (shared by the cells' figures.py)."""
from __future__ import annotations

import json
import statistics as st

from ..config import Config


def stats(xs) -> dict:
    """median / min / max / n of the non-None values ({} if none)."""
    xs = [x for x in xs if x is not None]
    return {"median": st.median(xs), "min": min(xs), "max": max(xs), "n": len(xs)} if xs else {}


def need(inputs: dict[str, list[str]] | None, *names: str) -> list[list[str]]:
    """The values of the named --input arguments; which results a report uses is never defaulted."""
    missing = [n for n in names if not (inputs or {}).get(n)]
    if missing:
        raise SystemExit("missing " + " ".join(f"--input {n}=..." for n in missing)
                         + "  (the report directory's report.sh has the full command)")
    return [inputs[n] for n in names]


def experiment_runs(cfg: Config, experiments: list[str]) -> list[dict]:
    """summary.json of every run of the named experiments (`<timestamp>_<query>_<label>`), with its place
    in the experiment (position, prev, experiment id, controller-cache eviction)."""
    root = cfg.repo / cfg.RESULTS_DIR
    out = []
    for name in experiments:
        doc = json.loads((root / "experiments" / f"{name}.json").read_text())
        for o in doc["order"]:
            s = json.loads((root / "runs" / o["run"] / "summary.json").read_text())
            s.update(position=o["position"], prev=o["prev"], experiment=doc["experiment"],
                     evicted=doc.get("device_cache_evicted_GiB", 0), run=o["run"])
            out.append(s)
    return out
