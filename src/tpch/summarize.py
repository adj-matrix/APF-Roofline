"""Turn a run directory's raw logs into timeline.csv and summary.json.

Pure post-processing: can be re-run at any time on old run directories.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .parse.perf import parse_perf_intervals, parse_perf_stat, perf_metrics
from .parse.plan import parse_plan
from .parse.sysstat import parse_iostat, parse_mpstat, parse_pidstat

TIMELINE_COLUMNS = [
    "t_s",
    "cpu_user_pct", "cpu_sys_pct", "cpu_iowait_pct", "cpu_idle_pct",          # whole machine
    "backend_cpu_pct", "backend_usr_pct", "backend_sys_pct", "backend_read_kBps",
    "dev_r_per_s", "dev_rMBps", "dev_rareq_kB", "dev_r_await_ms",
    "dev_wMBps", "dev_aqu_sz", "dev_util_pct",
    "perf_task_ms", "perf_cycles", "perf_user_cycles", "perf_instructions",   # backend, per second (-I)
]
PERF_COLUMNS = {"task-clock": "perf_task_ms", "cycles": "perf_cycles", "cycles:u": "perf_user_cycles",
                "instructions": "perf_instructions"}


def _read(run: Path, name: str) -> str:
    p = run / name
    return p.read_text() if p.exists() else ""


def build_timeline(run: Path, meta: dict) -> list[dict]:
    """One row per sample second; t_s is the end of the sample interval, relative to query start."""
    t0 = meta["t_start"]
    rows: dict[int, dict] = {}

    def at(ts: float) -> dict:   # key on the absolute second all tools print
        return rows.setdefault(int(ts), {"t_s": round(int(ts) - t0, 3)})

    for r in parse_mpstat(_read(run, "mpstat.log")):
        if r["CPU"] == "all":
            at(r["ts"]).update(
                cpu_user_pct=r["%usr"] + r["%nice"],
                cpu_sys_pct=r["%sys"] + r["%irq"] + r["%soft"],
                cpu_iowait_pct=r["%iowait"],
                cpu_idle_pct=r["%idle"],
            )
    for r in parse_pidstat(_read(run, "pidstat.log")):
        at(r["ts"]).update(
            backend_cpu_pct=r["%CPU"], backend_usr_pct=r["%usr"],
            backend_sys_pct=r["%system"], backend_read_kBps=r["kB_rd/s"],
        )
    for r in parse_iostat(_read(run, "iostat.log"), device=meta["device"]):
        at(r["ts"]).update(
            dev_r_per_s=r["r/s"], dev_rMBps=r["rkB/s"] / 1024, dev_rareq_kB=r["rareq-sz"],
            dev_r_await_ms=r["r_await"], dev_wMBps=r["wkB/s"] / 1024,
            dev_aqu_sz=r["aqu-sz"], dev_util_pct=r["%util"],
        )
    if meta.get("perf_interval_ms") and meta.get("perf_t0"):
        # perf's interval i ends perf_t0 + t; key it on the second it ends in, like the sysstat samples
        for r in parse_perf_intervals(_read(run, "perf.log")):
            at(meta["perf_t0"] + r["t"]).update(
                {col: r["events"][ev][0] for ev, col in PERF_COLUMNS.items() if ev in r["events"]})
    return [rows[k] for k in sorted(rows)]


def _overlap(t_s: float, interval: float, wall: float) -> float:
    """Fraction of a sample's interval that lies inside the query [0, wall].

    sysstat prints truncated whole seconds, so a sample stamped t ends somewhere in
    [t, t+1); assume the middle.
    """
    end = t_s + 0.5
    return max(0.0, min(end, wall) - max(end - interval, 0.0)) / interval


def summarize(run: Path) -> dict:
    run = Path(run)
    meta = json.loads((run / "meta.json").read_text())
    interval = meta["interval_s"]
    wall = meta["t_end"] - meta["t_start"]
    timeline = build_timeline(run, meta)

    with open(run / "timeline.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TIMELINE_COLUMNS, restval="")
        w.writeheader()
        w.writerows(timeline)

    c = meta.get("counters")
    if c:   # exact kernel counter deltas taken around the query
        d = c["disk"]
        read_kb = d["sectors_read"] * 512 / 1024
        reads = d["reads"]
        dev_busy_s = d["io_ticks_ms"] / 1000
        read_ms = d.get("read_ms")
        p = c.get("proc", {})
        backend_usr_s, backend_sys_s = p.get("utime_s"), p.get("stime_s")
        backend_cpu_s = None if backend_usr_s is None else backend_usr_s + backend_sys_s
        source = "counters"
    else:   # older runs: integrate 1 s samples, weighted by overlap with the query
        w = [(r, _overlap(r["t_s"], interval, wall)) for r in timeline]
        s = lambda col: sum(r.get(col, 0.0) * f for r, f in w) * interval  # noqa: E731
        read_kb = s("dev_rMBps") * 1024
        reads = s("dev_r_per_s")
        dev_busy_s = s("dev_util_pct") / 100
        read_ms = None
        backend_usr_s, backend_sys_s = s("backend_usr_pct") / 100, s("backend_sys_pct") / 100
        backend_cpu_s = s("backend_cpu_pct") / 100
        source = "samples"

    summary = {
        k: meta[k] for k in ("query", "sf", "cache", "mode", "label", "repeat_idx", "settings")
    }
    summary.update(
        wall_s=wall,
        short_run=wall < 5 * interval,   # 1 s sampling is too coarse to trust the rates below
        dev_read_MiB=read_kb / 1024,
        dev_avg_read_MiBps=read_kb / 1024 / wall if wall else None,
        dev_avg_rareq_kB=read_kb / reads if reads else None,
        dev_busy_frac=dev_busy_s / wall if wall else None,
        # HDD ~1 ms per 128 kB request; RAID controller cache hit ~0.1 ms
        dev_avg_r_await_ms=read_ms / reads if read_ms is not None and reads else None,
        backend_cpu_s=backend_cpu_s,
        backend_usr_s=backend_usr_s,
        backend_sys_s=backend_sys_s,
        backend_cpu_util=backend_cpu_s / wall if wall and backend_cpu_s is not None else None,
        totals_source=source,
    )
    if (run / "perf.log").exists():   # the backend is idle outside the query: counts ~ the query
        summary.update(perf_metrics(parse_perf_stat((run / "perf.log").read_text(),
                                                    interval=bool(meta.get("perf_interval_ms")))))
    if (run / "plan.json").exists():
        plan = parse_plan(json.loads((run / "plan.json").read_text()))
        root = plan["nodes"][0]
        summary.update(
            exec_ms=plan["execution_ms"], planning_ms=plan["planning_ms"],
            shared_hit_blks=root["shared_hit_blks"], shared_read_blks=root["shared_read_blks"],
            temp_read_blks=root["temp_read_blks"], temp_written_blks=root["temp_written_blks"],
            io_read_ms=root["io_read_ms"],
            plan_nodes=plan["nodes"],
        )
    (run / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
