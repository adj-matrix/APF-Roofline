"""Loading measured runs for figures (shared by the cells' figures.py)."""
from __future__ import annotations

import json
import statistics as st

from ..config import Config


def stats(xs) -> dict:
    """median / min / max / n of the non-None values ({} if none)."""
    xs = [x for x in xs if x is not None]
    return {"median": st.median(xs), "min": min(xs), "max": max(xs), "n": len(xs)} if xs else {}


def experiment_runs(cfg: Config, query: str, label: str) -> list[dict]:
    """summary.json of every run of every experiment `*_<query>_<label>.json`, with its place in
    the experiment (position, prev, experiment id, controller-cache eviction)."""
    root = cfg.repo / cfg.RESULTS_DIR
    out = []
    for f in sorted((root / "experiments").glob(f"*_{query}_{label}.json")):
        doc = json.loads(f.read_text())
        for o in doc["order"]:
            s = json.loads((root / "runs" / o["run"] / "summary.json").read_text())
            s.update(position=o["position"], prev=o["prev"], experiment=doc["experiment"],
                     evicted=doc.get("device_cache_evicted_GiB", 0), run=o["run"])
            out.append(s)
    return out
