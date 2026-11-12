"""`perf stat -x,` CSV output -> {event: {"value", "unit", "running_pct"}}.

Line format: value,unit,event,run_time_ns,running_pct[,metric,metric_unit]
running_pct < 100 means the counter was multiplexed and the value is scaled (less exact).
Values "<not counted>" / "<not supported>" become None. Non-CSV lines are ignored.
"""
from __future__ import annotations


def parse_perf_stat(text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in text.splitlines():
        f = line.split(",")
        if len(f) < 5 or not f[2] or line.startswith("#"):
            continue
        try:
            value = float(f[0])
        except ValueError:
            value = None
        try:
            pct = float(f[4])
        except ValueError:
            pct = None
        out[f[2]] = {"value": value, "unit": f[1], "running_pct": pct}
    return out


def perf_metrics(events: dict[str, dict]) -> dict:
    """Derived metrics for summary.json (None when an input is missing)."""
    v = lambda name: (events.get(name) or {}).get("value")  # noqa: E731
    tc_ms, cyc, ins, ref = v("task-clock"), v("cycles"), v("instructions"), v("ref-cycles")
    cyc_u, ins_u = v("cycles:u"), v("instructions:u")
    div = lambda a, b: a / b if a is not None and b else None  # noqa: E731
    pcts = [e["running_pct"] for e in events.values() if e.get("running_pct") is not None]
    return {
        "perf_task_clock_s": tc_ms / 1000 if tc_ms is not None else None,
        "perf_cycles": cyc,
        "perf_instructions": ins,
        "perf_ipc": div(ins, cyc),
        "perf_GHz": div(cyc, tc_ms * 1e6 if tc_ms else None),   # average clock while on CPU
        "perf_freq_ratio": div(cyc, ref),                          # actual / nominal (TSC) clock
        "perf_user_cycles_frac": div(cyc_u, cyc),
        "perf_user_instructions": ins_u,
        "perf_context_switches": v("context-switches"),
        "perf_cpu_migrations": v("cpu-migrations"),
        "perf_major_faults": v("major-faults"),
        "perf_llc_loads": v("LLC-loads"),                          # demand loads that missed L2
        "perf_llc_load_misses": v("LLC-load-misses"),              # ... and also missed L3 (went to DRAM)
        "perf_min_running_pct": min(pcts) if pcts else None,       # < 100: multiplexed, scaled
    }
