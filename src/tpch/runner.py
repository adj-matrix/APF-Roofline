"""Run one query under controlled conditions; each repetition produces one run directory.

results/runs/<YYYYmmdd-HHMMSS>_<query>_sf<N>_<mode>_<cache>[_<label>]_r<i>/
  meta.json  plan.json (analyze modes) | result.json (plain)  iostat/mpstat/pidstat/perf.log
  timeline.csv  summary.json
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import cache, counters, sysinfo
from .config import Config
from .monitor import Monitors
from .shell import Shell
from .summarize import summarize

MODES = {
    "plain": "",                                                     # true wall time
    "analyze": "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ",           # per-node times (has overhead)
    "analyze_notiming": "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) ",
}
CACHES = ("cold", "warm")
# Recorded in meta.json for every run, whatever their source.
RECORDED_SETTINGS = [
    "server_version", "shared_buffers", "work_mem", "effective_io_concurrency", "io_method",
    "io_combine_limit", "jit", "max_parallel_workers_per_gather", "track_io_timing",
    "random_page_cost", "seq_page_cost", "enable_hashjoin", "enable_mergejoin", "enable_nestloop",
]


def query_sql(cfg: Config, query: str) -> str:
    """`q6` -> queries/q6/query.sql. `q1.m_filter` -> the statement after `-- name: m_filter` in
    queries/q1/micro.sql (microbenchmark queries that take a query apart)."""
    base, _, part = query.partition(".")
    if part:
        text = micro_statement((cfg.repo / "queries" / base / "micro.sql").read_text(), part)
    else:
        text = (cfg.repo / "queries" / query / "query.sql").read_text()
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    return "\n".join(lines).strip().rstrip(";")


def micro_statement(text: str, name: str) -> str:
    sections: dict[str, list[str]] = {}
    current = None
    for ln in text.splitlines():
        if ln.startswith("-- name:"):
            current = ln.split(":", 1)[1].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(ln)
    if name not in sections:
        raise KeyError(f"no '-- name: {name}' section")
    return "\n".join(sections[name])


def run_dir_name(query: str, sf: int, mode: str, cache_state: str, label: str, i: int) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    lab = f"_{label}" if label else ""
    return f"{stamp}_{query}_sf{sf}_{mode}_{cache_state}{lab}_r{i}"


def run_query(cfg: Config, query: str, sf: int, cache_state: str = "cold", mode: str = "plain",
              label: str = "", repeat: int = 1, settings: dict[str, str] | None = None,
              dry_run: bool = False, start_idx: int = 0, extra_meta: dict | None = None,
              sh: Shell | None = None) -> tuple[list[Path], Shell]:
    """Run `repeat` measured repetitions, indices start_idx.. ; extra_meta is merged into meta.json."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    if cache_state not in CACHES:
        raise ValueError(f"cache must be one of {CACHES}")
    settings = settings or {}
    sql = MODES[mode] + query_sql(cfg, query)
    sh = sh or Shell(dry_run)
    runs: list[Path] = []
    results = cfg.repo / cfg.RESULTS_DIR / "runs"

    for i in range(start_idx, start_idx + repeat):
        run = results / run_dir_name(query, sf, mode, cache_state, label, i)
        if cache_state == "cold":
            cache.make_cold(cfg, sh)
        if dry_run:
            sh.log.append(f"[psql {cfg.dbname(sf)}] " + "; ".join(
                [f"SET {k} = {v}" for k, v in settings.items()]
                + (["<warm-up: query once>"] if cache_state == "warm" else []) + [sql]))
            runs.append(run)
            continue
        _measure(cfg, sf, run, sql, mode, cache_state, settings,
                 meta={"query": query, "sf": sf, "cache": cache_state, "mode": mode,
                       "label": label, "repeat_idx": i, **(extra_meta or {})})
        summarize(run)
        runs.append(run)
    return runs, sh


def _measure(cfg: Config, sf: int, run: Path, sql: str, mode: str, cache_state: str,
             settings: dict[str, str], meta: dict) -> None:
    import psycopg  # imported here so dry runs and tests need no database driver

    interval = cfg.int("SAMPLE_INTERVAL")
    run.mkdir(parents=True)
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname=cfg.dbname(sf), autocommit=True) as conn:
        cur = conn.cursor()
        for k, v in settings.items():
            cur.execute("SELECT set_config(%s, %s, false)", (k, v))
        if cache_state == "warm":
            cur.execute(sql)
            cur.fetchall()
        pid = cur.execute("SELECT pg_backend_pid()").fetchone()[0]
        pg_settings = dict(cur.execute(
            "SELECT name, current_setting(name) FROM pg_settings WHERE name = ANY(%s)",
            (RECORDED_SETTINGS,)).fetchall())

        perf_events = cfg.PERF_EVENTS if cfg.int("PERF_STAT") else None
        device = cfg.device(sf)             # sda for SF1/SF10, sdb for SF >= BIG_SF_MIN
        with Monitors(run, device=device, pid=pid, interval=interval, perf_events=perf_events):
            time.sleep(interval)            # one idle sample before the query starts (perf attaches)
            c0 = counters.snapshot(device, pid)
            t_start = time.time()
            cur.execute(sql)
            rows = cur.fetchall()
            t_end = time.time()
            c1 = counters.snapshot(device, pid)
            time.sleep(interval + 0.2)      # let the samplers report the last interval

    if mode == "plain":
        (run / "result.json").write_text(json.dumps(rows, default=str, indent=2))
    else:
        (run / "plan.json").write_text(json.dumps(rows[0][0], indent=2))
    meta.update(
        t_start=t_start, t_end=t_end, counters=counters.delta(c0, c1), backend_pid=pid, device=device, interval_s=interval,
        settings=settings, pg_settings=pg_settings, sql=sql, config=cfg.values, sys=sysinfo.collect(),
    )
    (run / "meta.json").write_text(json.dumps(meta, indent=2))
