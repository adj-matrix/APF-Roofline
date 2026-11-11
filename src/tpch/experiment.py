"""Interleaved measurements of several conditions: a randomized block design.

Comparing conditions run one after another confounds them with drift over time
(report #1: B1 vs B2). An experiment instead runs B blocks; every block contains each
condition (treatment) once, in a freshly shuffled order:

    ./tpch experiment --query q6 --sf 1 10 --cache cold warm --blocks 5 --label scale

Terms (design of experiments): experiment > block > run; a condition is one
(SF, cache, mode) combination. The order, the seed and every run directory go to
results/experiments/<stamp>_<query>_<label>.json, rewritten after each run, so an interrupted
experiment still documents what ran. Each run's meta.json gets
{"experiment", "position", "block", "prev"}; "prev" is the condition that ran just before,
i.e. what the caches (OS, RAID controller) last held.
"""
from __future__ import annotations

import itertools
import json
import random
from datetime import datetime
from pathlib import Path

from . import cache
from .config import Config
from .runner import run_query
from .shell import Shell


def conditions(sfs: list[int], caches: list[str], modes: list[str]) -> list[dict]:
    return [{"sf": sf, "cache": c, "mode": m} for sf, c, m in itertools.product(sfs, caches, modes)]


def schedule(conds: list[dict], blocks: int, seed: int) -> list[dict]:
    """Randomized blocks: each block is a fresh shuffle of all conditions."""
    rng = random.Random(seed)
    out = []
    for b in range(blocks):
        order = list(conds)
        rng.shuffle(order)
        out += [{**c, "block": b} for c in order]
    return out


def _key(c: dict | None) -> str | None:
    return None if c is None else f"sf{c['sf']}_{c['mode']}_{c['cache']}"


def run_experiment(cfg: Config, query: str, conds: list[dict], blocks: int, seed: int | None = None,
                   label: str = "", settings: dict[str, str] | None = None,
                   dry_run: bool = False) -> tuple[Path, Shell]:
    seed = seed if seed is not None else random.SystemRandom().randrange(2**31)
    order = schedule(conds, blocks, seed)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    eid = f"{stamp}_{query}_{label}" if label else f"{stamp}_{query}"
    path = cfg.repo / cfg.RESULTS_DIR / "experiments" / f"{eid}.json"
    doc = {"experiment": eid, "query": query, "label": label, "seed": seed, "blocks": blocks,
           "conditions": conds, "settings": settings or {}, "order": []}
    sh = Shell(dry_run)
    if any(c["cache"] == "cold" for c in conds):
        cache.evict_device_cache(cfg, sh)       # reads never refill it: once per experiment suffices
        doc["device_cache_evicted_GiB"] = cfg.int("DEVICE_CACHE_EVICT_GIB")
    prev = None
    for pos, item in enumerate(order):
        extra = {"experiment": eid, "position": pos, "block": item["block"], "prev": _key(prev)}
        runs, _ = run_query(cfg, query, item["sf"], item["cache"], item["mode"], label, 1,
                            settings, dry_run=dry_run, start_idx=item["block"], extra_meta=extra, sh=sh)
        doc["order"].append({**item, "position": pos, "prev": extra["prev"], "run": runs[0].name})
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc, indent=2))
        prev = item
    return path, sh
