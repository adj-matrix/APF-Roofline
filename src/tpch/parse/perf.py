"""`perf stat -x,` CSV output -> {event: {"value", "unit", "running_pct"}}.

Line format: value,unit,event,run_time_ns,running_pct[,metric,metric_unit]
With -I (interval mode) every line starts with the seconds since perf started: time,value,unit,event,...;
the counts are per interval, and their sum is the total.
running_pct < 100 means the counter was multiplexed and the value is scaled (less exact).
Values "<not counted>" / "<not supported>" become None. Non-CSV lines are ignored.
"""
from __future__ import annotations


def parse_perf_stat(text: str, interval: bool = False) -> dict[str, dict]:
    """Totals per event (interval mode: summed over the intervals, running_pct the lowest)."""
    if interval:
        out = {}
        for row in parse_perf_intervals(text):
            for ev, (value, pct) in row["events"].items():
                o = out.setdefault(ev, {"value": None, "unit": row["units"][ev], "running_pct": pct})
                if value is not None:
                    o["value"] = (o["value"] or 0.0) + value
                if pct is not None and (o["running_pct"] is None or pct < o["running_pct"]):
                    o["running_pct"] = pct
        return out
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


def parse_perf_intervals(text: str) -> list[dict]:
    """Interval mode (-I): [{"t": seconds since perf started, "events": {event: (value, running_pct)},
    "units": {event: unit}}], one per interval, in order. A counter that did not run in an interval
    ("<not counted>": the process was off CPU throughout) is 0."""
    rows: dict[float, dict] = {}
    for line in text.splitlines():
        f = line.strip().split(",")
        if len(f) < 6 or not f[3] or line.startswith("#"):
            continue
        try:
            t = float(f[0])
        except ValueError:
            continue
        try:
            value = float(f[1])
        except ValueError:
            value = 0.0 if f[1] == "<not counted>" else None
        try:
            pct = float(f[5])
        except ValueError:
            pct = None
        r = rows.setdefault(t, {"t": t, "events": {}, "units": {}})
        r["events"][f[3]] = (value, pct)
        r["units"][f[3]] = f[2]
    return [rows[t] for t in sorted(rows)]


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
