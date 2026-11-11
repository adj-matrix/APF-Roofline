"""summarize() on a synthetic run: 3 s query reading 100 MiB/s in 128 KiB requests, backend at 50% CPU.

The query runs [T+0.5, T+3.5]; sysstat stamps truncated seconds, so the busy samples are
stamped T+1..T+3 and (by the "assume mid-second" convention) cover exactly the query.
"""
import csv

import pytest
import json
from datetime import datetime

from tpch.summarize import summarize

T = 1_800_000_000  # query start (epoch seconds)
SAMPLES = range(0, 5)  # sample ends at T+0 (idle lead) .. T+4 (tail)


def busy(k: int) -> bool:
    return 1 <= k <= 3


def iostat_text() -> str:
    out = ["Linux 5.15.0 (host) \t2027-01-15 \t_x86_64_\t(32 CPU)", ""]
    for k in SAMPLES:
        rkb, rs = (102400.0, 800.0) if busy(k) else (0.0, 0.0)
        out += [datetime.fromtimestamp(T + k).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                "Device r/s rkB/s rareq-sz r_await wkB/s aqu-sz %util",
                f"sda {rs} {rkb} {128.0 if rs else 0.0} 0.5 0.0 1.0 {90.0 if rs else 0.0}", ""]
    return "\n".join(out)


def mpstat_text() -> str:
    day = datetime.fromtimestamp(T).astimezone().strftime("%Y-%m-%d")
    out = [f"Linux 5.15.0 (host) \t{day} \t_x86_64_\t(32 CPU)", ""]
    for k in SAMPLES:
        hms = datetime.fromtimestamp(T + k).astimezone().strftime("%H:%M:%S")
        out += [f"{hms} CPU %usr %nice %sys %iowait %irq %soft %idle",
                f"{hms} all 2.0 0.0 1.0 3.0 0.0 0.0 94.0", ""]
    return "\n".join(out)


def pidstat_text() -> str:
    out = ["Linux 5.15.0 (host) \t2027-01-15 \t_x86_64_\t(32 CPU)", ""]
    for k in SAMPLES:
        cpu = 50.0 if busy(k) else 0.0
        out += ["# Time UID PID %usr %system %CPU kB_rd/s Command",
                f"{T + k} 1000 42 {cpu - 10 if cpu else 0.0} {10.0 if cpu else 0.0} {cpu} 0.0 postgres", ""]
    return "\n".join(out)


def make_run(tmp_path, plan=None, counters=None):
    run = tmp_path / "run"
    run.mkdir()
    (run / "iostat.log").write_text(iostat_text())
    (run / "mpstat.log").write_text(mpstat_text())
    (run / "pidstat.log").write_text(pidstat_text())
    meta = {"query": "q6", "sf": 10, "cache": "cold", "mode": "plain", "label": "", "repeat_idx": 0,
            "settings": {}, "t_start": T + 0.5, "t_end": T + 3.5, "device": "sda", "interval_s": 1}
    if counters:
        meta["counters"] = counters
    (run / "meta.json").write_text(json.dumps(meta))
    if plan:
        (run / "plan.json").write_text(json.dumps(plan))
    return run


def test_window_aggregates(tmp_path):
    s = summarize(make_run(tmp_path))
    assert s["wall_s"] == 3.0
    assert s["dev_read_MiB"] == 300.0              # 3 busy samples x 100 MiB/s
    assert s["dev_avg_read_MiBps"] == 100.0
    assert s["dev_avg_rareq_kB"] == 128.0
    assert s["backend_cpu_s"] == 1.5
    assert s["backend_cpu_util"] == 0.5
    assert s["short_run"] is True
    assert s["dev_busy_frac"] == 0.9 and s["totals_source"] == "samples"


def test_timeline_csv_includes_lead_and_tail(tmp_path):
    run = make_run(tmp_path)
    summarize(run)
    rows = list(csv.DictReader(open(run / "timeline.csv")))
    assert [float(r["t_s"]) for r in rows] == [-0.5, 0.5, 1.5, 2.5, 3.5]
    assert float(rows[2]["dev_rMBps"]) == 100.0
    assert float(rows[2]["backend_cpu_pct"]) == 50.0 and float(rows[2]["cpu_iowait_pct"]) == 3.0


def test_plan_fields_when_analyzed(tmp_path):
    plan = [{"Plan": {"Node Type": "Seq Scan", "Actual Total Time": 2900.0, "Actual Loops": 1,
                      "Shared Read Blocks": 38400, "Shared I/O Read Time": 1200.0}},
            ][0:1]
    plan[0].update({"Planning Time": 0.3, "Execution Time": 2950.0})
    s = summarize(make_run(tmp_path, plan))
    assert s["exec_ms"] == 2950.0 and s["shared_read_blks"] == 38400 and s["io_read_ms"] == 1200.0
    assert s["plan_nodes"][0]["type"] == "Seq Scan"


def test_exact_counters_take_precedence(tmp_path):
    counters = {"disk": {"reads": 2400, "sectors_read": 300 * 2048, "io_ticks_ms": 2970, "read_ms": 2640},
                "proc": {"utime_s": 1.2, "stime_s": 0.3, "read_bytes": 0, "rchar": 0}}
    s = summarize(make_run(tmp_path, counters=counters))
    assert s["totals_source"] == "counters"
    assert s["dev_read_MiB"] == 300.0 and s["dev_avg_rareq_kB"] == 128.0
    assert s["dev_busy_frac"] == pytest.approx(0.99)
    assert s["dev_avg_r_await_ms"] == pytest.approx(1.1)
    assert (s["backend_usr_s"], s["backend_sys_s"], s["backend_cpu_s"]) == (1.2, 0.3, 1.5)
