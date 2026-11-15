"""Interleaved measurements of several conditions: a randomized block design.

Comparing conditions run one after another confounds them with drift over time
(report #1: B1 vs B2). An experiment instead runs B blocks; every block contains each
condition (treatment) once, in a freshly shuffled order:

    ./tpch experiment --query q6 --sf 1 10 --cache cold warm --blocks 5 --label scale

Terms (design of experiments): experiment > block > run; a condition is one
(SF, cache, mode[, query][, cpufreq]) combination. The order, the seed and every run directory go to
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

from . import cache, sysconf
from .config import Config
from .runner import run_query
from .shell import Shell


def conditions(sfs: list[int], caches: list[str], modes: list[str],
               queries: list[str] | None = None, cpufreqs: list[str] | None = None) -> list[dict]:
    """Cross product. With several queries (e.g. microbenchmark parts), the query is a factor too;
    with several CPU frequency settings (`governor:no_turbo`, set before each run), so is that."""
    base = [{"sf": sf, "cache": c, "mode": m} for sf, c, m in itertools.product(sfs, caches, modes)]
    if queries and len(queries) > 1:
        base = [{"query": q, **c} for q in queries for c in base]
    if cpufreqs:
        for f in cpufreqs:
            sysconf.parse_cpufreq(f)
        base = [{"cpufreq": f, **c} for f in cpufreqs for c in base]
    return base


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
    if c is None:
        return None
    k = f"sf{c['sf']}_{c['mode']}_{c['cache']}"
    k = f"{c['query']}_{k}" if "query" in c else k
    return f"{k}_{c['cpufreq']}" if "cpufreq" in c else k


def run_experiment(cfg: Config, query: str, conds: list[dict], blocks: int, seed: int | None = None,
                   label: str = "", settings: dict[str, str] | None = None,
                   dry_run: bool = False) -> tuple[Path, Shell]:
    seed = seed if seed is not None else random.SystemRandom().randrange(2**31)
    order = schedule(conds, blocks, seed)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = query.partition(".")[0]          # q1.m_filter etc. belong to q1's experiments
    eid = f"{stamp}_{name}_{label}" if label else f"{stamp}_{name}"
    path = cfg.repo / cfg.RESULTS_DIR / "experiments" / f"{eid}.json"
    doc = {"experiment": eid, "query": query, "label": label, "seed": seed, "blocks": blocks,
           "conditions": conds, "settings": settings or {}, "order": []}
    sh = Shell(dry_run)
    if any(c["cache"] == "cold" for c in conds):
        roots = sorted({cfg.root(c["sf"]) for c in conds if c["cache"] == "cold"})
        cache.evict_device_cache(cfg, sh, roots)  # reads never refill it: once per experiment suffices
        doc["device_cache_evicted_GiB"] = cfg.int("DEVICE_CACHE_EVICT_GIB")
        doc["device_cache_evicted_on"] = roots
    if any("cpufreq" in c for c in conds) and "cpufreq" not in sysconf.knobs(cfg):
        raise ValueError("--cpufreq needs the cpufreq knob in SYSCONF (it restores the setting at the end)")
    prev = None
    for pos, item in enumerate(order):
        if "cpufreq" in item and (prev is None or prev.get("cpufreq") != item["cpufreq"]):
            sysconf.set_cpufreq(sh, *sysconf.parse_cpufreq(item["cpufreq"]))
        extra = {"experiment": eid, "position": pos, "block": item["block"], "prev": _key(prev)}
        if "cpufreq" in item:
            extra["cpufreq"] = item["cpufreq"]
        runs, _ = run_query(cfg, item.get("query", query), item["sf"], item["cache"], item["mode"], label, 1,
                            settings, dry_run=dry_run, start_idx=item["block"], extra_meta=extra, sh=sh)
        doc["order"].append({**item, "position": pos, "prev": extra["prev"], "run": runs[0].name})
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc, indent=2))
        prev = item
    return path, sh
