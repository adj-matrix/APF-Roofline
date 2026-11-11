from pathlib import Path

import pytest

from tpch.monitor import Monitors, perf_stat_cmd
from tpch.parse.perf import parse_perf_stat, perf_metrics

FIX = Path(__file__).parent / "fixtures"


def test_parse_real_output():
    ev = parse_perf_stat((FIX / "perf_stat.csv").read_text())
    assert len(ev) == 10
    assert ev["task-clock"] == {"value": 132.06, "unit": "msec", "running_pct": 100.0}
    assert ev["cycles"]["value"] == 387471188
    assert ev["cycles:u"]["value"] == 1180679


def test_metrics_real_output():
    m = perf_metrics(parse_perf_stat((FIX / "perf_stat.csv").read_text()))
    assert m["perf_task_clock_s"] == pytest.approx(0.13206)
    assert m["perf_ipc"] == pytest.approx(1087045178 / 387471188)
    assert m["perf_GHz"] == pytest.approx(387471188 / 132.06e6)          # perf printed 2.934
    assert m["perf_freq_ratio"] == pytest.approx(387471188 / 354508326)
    assert m["perf_user_cycles_frac"] == pytest.approx(1180679 / 387471188)
    assert m["perf_min_running_pct"] == 100.0


def test_not_counted_and_noise_lines():
    text = "\n# started on ...\n<not counted>,,cycles,0,0.00,,\n12.5,msec,task-clock,12500000,100.00,1.0,CPUs utilized\n"
    ev = parse_perf_stat(text)
    assert ev["cycles"]["value"] is None
    m = perf_metrics(ev)
    assert m["perf_cycles"] is None and m["perf_ipc"] is None and m["perf_GHz"] is None
    assert m["perf_task_clock_s"] == pytest.approx(0.0125)


def test_perf_only_with_pid_and_events(tmp_path):
    assert "perf" not in Monitors(tmp_path, "sda", pid=None, perf_events="cycles").cmds
    assert "perf" not in Monitors(tmp_path, "sda", pid=42).cmds
    cmd = Monitors(tmp_path, "sda", pid=42, perf_events="cycles,instructions").cmds["perf"]
    assert cmd == perf_stat_cmd(42, "cycles,instructions")
    assert cmd[-7:] == ["perf", "stat", "-x,", "-e", "cycles,instructions", "-p", "42"]
    assert "LC_ALL=C" in cmd
    i = cmd.index("sh")              # shell prints its PID, then exec's perf (same PID)
    assert cmd[i:i + 4] == ["sh", "-c", 'echo $$; exec "$@"', "sh"]
