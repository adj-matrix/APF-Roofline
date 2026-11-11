import pytest

from tpch.model.common import HwParams, TableStats
from queries.q6.model import predict_q6

# 1 GiB table (131072 pages), 10M tuples; 128 MiB/s disk; 100 ns/tuple.
T = TableStats("lineitem", pages=131072, tuples=10_000_000)
HW = HwParams(seq_read_MiBps=128.0, cpu_ns_per_tuple=100.0)


def test_cold_io_bound():
    p = predict_q6(T, HW)
    assert p.components == pytest.approx({"io_s": 8.0, "cpu_s": 1.0})   # 1024/128 ; 1e7*100ns
    assert (p.lower_s, p.upper_s, p.total_s) == pytest.approx((8.0, 9.0, 8.0))
    assert p.bound == "io"


def test_sum_combination_is_upper_bound():
    assert predict_q6(T, HW, how="sum").total_s == pytest.approx(9.0)


def test_warm_is_cpu_only():
    p = predict_q6(T, HW, cache="warm")
    assert p.components["io_s"] == 0.0 and p.total_s == pytest.approx(1.0) and p.bound == "cpu"


def test_cpu_bound_when_disk_is_fast():
    p = predict_q6(T, HwParams(seq_read_MiBps=4096.0, cpu_ns_per_tuple=100.0))
    assert p.bound == "cpu" and p.total_s == pytest.approx(1.0)


def test_assumptions_are_stated():
    assert any("Seq Scan" in a for a in predict_q6(T, HW).assumptions)
