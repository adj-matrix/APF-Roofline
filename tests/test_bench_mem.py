import pytest

from tpch import config
from tpch.bench.mem import accesses_for, default_sizes, latency_at, memlat_cmd, run_mem_bench


def test_sizes_span_l1_to_dram():
    s = default_sizes()
    assert s[0] == 4096 and s[-1] == 1 << 30 and s == sorted(s) and len(s) == len(set(s))


def test_every_size_is_passed_over_several_times():
    assert accesses_for(1 << 30) == 4 * (1 << 30) // 64
    assert accesses_for(4096) == 5_000_000


def test_placement_binds_cpu_and_memory():
    cmd = memlat_cmd("/x/memlat", "remote", 1 << 20)
    assert cmd[:3] == ["numactl", "--cpunodebind=0", "--membind=1"]


def test_latency_interpolates_in_log_size():
    curve = {1 << 20: 10.0, 1 << 22: 30.0}
    assert latency_at(curve, 1 << 21) == pytest.approx(20.0)
    assert latency_at(curve, 1 << 10) == 10.0 and latency_at(curve, 1 << 30) == 30.0


def test_dry_run_compiles_then_runs_each_placement_and_size(tmp_path):
    cfg = config.load(env={"RESULTS_DIR": str(tmp_path)})
    _, sh = run_mem_bench(cfg, reps=2, sizes=[4096, 8192], dry_run=True)
    assert sh.log[0].startswith("gcc -O2")
    assert sum(c.startswith("numactl") for c in sh.log) == 2 * 2 * 2
