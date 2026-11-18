"""The model's machine-independent parts and the cost solver, without measured data."""
import pytest

from tpch.model import pg
from tpch.model.cost import predict, temp_read_from_device_mib
from tpch.model.params import Param, ParamSet, interp
from tpch.model.plan import UnsupportedPlan, from_explain, pair
from tpch.model.registry import DATASETS
from tpch.model.workload import HashJoinSpill, Scan, Workload, build

MiB = 1 << 20


# --- PostgreSQL 18 hash-join sizing: the measured plans of Q3 (SF10 calibration, SF100) ----------------
@pytest.mark.parametrize("rows, wm, batches, buckets", [
    (7_289_442, "64MB", 4, 4_194_304),        # Q3 sf100_shape at SF10, 64MB (measured)
    (7_289_442, "128MB", 2, 8_388_608),       # SF10, 128MB
    (72_868_000, "64MB", 64, 4_194_304),      # SF100, 64MB
    (72_868_000, "128MB", 32, 8_388_608),     # SF100, 128MB
    (72_868_000, "256MB", 16, 16_777_216),    # SF100, 256MB
])
def test_hash_join_sizing_matches_measured_plans(rows, wm, batches, buckets):
    s = pg.choose_hash_table_size(rows, 16, pg.parse_mem(wm))
    assert (s.nbatch, s.nbuckets) == (batches, buckets)


def test_single_batch_when_it_fits():
    s = pg.choose_hash_table_size(3_000_000, 4, pg.parse_mem("128MB"))   # customer at SF100
    assert s.nbatch == 1 and s.nbuckets == 4_194_304


def test_tuple_sizes():
    assert pg.hash_entry_bytes(16) == 48          # orders: 16 + 16 + 16
    assert pg.spilled_tuple_bytes(16) == 36        # hash value + header + data
    assert pg.parse_mem("1GB") == 1 << 30 and pg.parse_mem(65536) == 64 << 20


# --- plans --------------------------------------------------------------------------------------------
def _plan(rows_scan, actual=None, node="Seq Scan"):
    p = {"Node Type": "Aggregate", "Strategy": "Plain", "Plan Rows": 1, "Plan Width": 32,
         "Plans": [{"Node Type": node, "Relation Name": "lineitem", "Plan Rows": rows_scan, "Plan Width": 20}]}
    if actual is not None:
        p["Actual Rows"], p["Actual Loops"] = 1, 1
        p["Plans"][0].update({"Actual Rows": actual, "Actual Loops": 1, "Temp Written Blocks": 7})
    return [{"Plan": p}]


def test_estimates_only_drops_actuals():
    n = from_explain(_plan(100, actual=90), estimates_only=True)
    assert all(x.actual_rows is None and "Temp Written Blocks" not in x.raw for x in n.walk())


def test_pairing_requires_the_same_shape():
    t, c = from_explain(_plan(1000)), from_explain(_plan(100, actual=90))
    assert len(pair(t, c)) == 2
    with pytest.raises(UnsupportedPlan):
        pair(from_explain(_plan(1000, node="Index Scan")), c)


def test_build_scales_rows_and_reads_catalog():
    t, c = from_explain(_plan(1000)), from_explain(_plan(100, actual=90))
    w = build("q6", t, c, 100, 10, {"lineitem": {"pages": 1000, "tuples": 50_000}}, {"work_mem": "64MB"})
    assert w.scale == 10 and w.table_pages == 1000 and w.spilled_tuples == 0


# --- parameters ---------------------------------------------------------------------------------------
def test_interp_is_clamped_and_log_linear():
    curve = [[16, 800.0], [64, 400.0]]
    assert interp(curve, 4) == 800.0 and interp(curve, 128) == 400.0
    assert interp(curve, 32) == pytest.approx(600.0)            # midpoint in log N


def test_param_kind_is_checked():
    with pytest.raises(ValueError):
        Param("x", 1.0, "s", "guess")


def test_digest_changes_with_values():
    a, b = ParamSet(), ParamSet()
    a.add(Param("x", 1.0, "s", "bench"))
    b.add(Param("x", 1.5, "s", "bench"))
    assert a.digest() != b.digest()


def test_every_test_dataset_is_kept_out_of_training():
    roles = {d.role for d in DATASETS.values()}
    assert roles <= {"train", "test", "blind"}
    assert all(not n.startswith(("q6_sf100", "q1_sf100")) or d.role in ("test", "blind") for n, d in DATASETS.items())


# --- cost ---------------------------------------------------------------------------------------------
def _params(clock=((0.9, 3.0), (1.0, 3.5))):
    P = ParamSet()
    for name, v in [("q.user_cycles_cal", 1e10), ("kernel_cycles_per_device_page", 10_000.0),
                    ("kernel_cycles_per_cached_page", 3_000.0), ("kernel_cycles_per_cpu_s", 0.0),
                    ("bw_big_MiBps", 1000.0), ("startup_wait_s", 1.0), ("page_cache_window_MiB", 1000.0),
                    ("scan_wait_frac", [[0.2, 0.0], [1.0, 0.0]]), ("clock_GHz_vs_on_cpu", [list(p) for p in clock])]:
        P.add(Param(name, v, "", "train"))
    return P


def test_cpu_bound_scan_is_cycles_over_clock_plus_startup():
    w = Workload("q", 100, 10, {}, 10.0, scans=[Scan("lineitem", 128_000, 1e6)])   # 1000 MiB
    p = predict(w, _params())
    cycles = 1e11 + 10_000 * 128_000
    assert p.cycles == pytest.approx(cycles)
    assert p.time_s == pytest.approx(cycles / (p.clock_ghz * 1e9) + 1.0, rel=1e-6)
    assert p.on_cpu_share == pytest.approx((p.time_s - 1.0) / p.time_s, rel=1e-6)   # clock and share consistent


def test_io_bound_scan_waits_for_the_device():
    w = Workload("q", 100, 10, {}, 0.001, scans=[Scan("lineitem", 12_800_000, 1e6)])   # 100,000 MiB, tiny CPU
    p = predict(w, _params())
    assert p.time_s == pytest.approx(100_000 / 1000 + 1.0, rel=1e-3)          # device time + start-up


def test_temp_from_device_keeps_the_tail_of_the_outer_files():
    j = HashJoinSpill("h", 64, 1, 0, 0, 16, 16, 0, temp_bytes=300 * MiB, outer_temp_bytes=200 * MiB,
                      outer_scan_mib=1800)
    w = Workload("q", 100, 10, {}, 10, joins=[j])
    # window 1000 MiB of a 2000 MiB stream, outer temp is 1/10 of it: 100 MiB cached, 200 from the device
    assert temp_read_from_device_mib(w, 1000) == pytest.approx(200)


def _spill_params():
    P = _params()
    for name, v in [("q.user_cycles_per_spilled_tuple", 0.0), ("q.kernel_cycles_per_temp_page", 0.0),
                    ("readback_bw_MiBps", [[64, 100.0]]), ("readback_batch_overhead_s", 0.0), ("burst_wait_per_burst_s", [[64, 0.0]]),
                    ("burst_s_per_GiB", 0.0), ("readback_cycles_share", 0.25),
                    ("readback_overlap_gamma", [[10.0, 0.5]]), ("clock_readback_GHz_vs_on_cpu", [[0.5, 2.0], [1.0, 2.0]])]:
        P.add(Param(name, v, "", "train"))
    P["page_cache_window_MiB"].value = 0.0                   # every temp page from the device
    return P


def test_readback_phase_is_max_plus_gamma_min():
    j = HashJoinSpill("h", 64, 1, 0, 0, 16, 16, 0, temp_bytes=1000 * MiB, outer_temp_bytes=800 * MiB,
                      outer_scan_mib=0)
    w = Workload("q", 100, 10, {}, 10.0, scans=[Scan("lineitem", 128_000, 1e6)], joins=[j])
    p = predict(w, _spill_params())
    device_s, cpu3 = 1000 / 100.0, p.parts["phase3_cpu_s"]
    assert p.parts["phase3_clock_GHz"] == pytest.approx(2.0)
    assert cpu3 == pytest.approx(p.parts["phase3_cycles"] / 2e9)
    assert p.parts["phase3_cycles"] + p.parts["phase12_cycles"] == pytest.approx(p.cycles)
    assert p.parts["phase3_s"] == pytest.approx(max(device_s, cpu3) + 0.5 * min(device_s, cpu3))
    assert p.time_s == pytest.approx(p.parts["phase12_s"] + p.parts["phase3_s"])


def test_batch_count_cycles_in_phase12_batch_size_cycles_in_phase3():
    def phase_cycles(write_curve, read_curve):
        P = _spill_params()
        P.add(Param("spill_write_user_cycles_vs_nbatch", write_curve, "", "train"))
        P.add(Param("spill_read_user_cycles_vs_batch_mib", read_curve, "", "train"))
        # 512 batches of 1e6 x 64 B / 512 = 0.12 MiB inner data each; 2e6 spilled tuples
        j = HashJoinSpill("h", 512, 1, 1e6, 1e6, 64, 16, 2e6, temp_bytes=1000 * MiB, outer_temp_bytes=800 * MiB,
                          outer_scan_mib=0)
        w = Workload("q", 100, 10, {}, 10.0, scans=[Scan("lineitem", 128_000, 1e6)], joins=[j])
        p = predict(w, P)
        return p.parts["phase12_cycles"], p.parts["phase3_cycles"], p
    c12, c3, _ = phase_cycles([[64, 0.0], [512, 0.0]], [[1.0, 0.0], [17.0, 0.0]])
    w12, w3, p = phase_cycles([[64, 0.0], [512, 100.0]], [[1.0, -50.0], [17.0, 0.0]])
    assert p.parts["U_write_nbatch_cycles"] == pytest.approx(100.0 * 2e6)
    assert p.parts["U_read_batch_size_cycles"] == pytest.approx(-50.0 * 2e6)     # 0.12 MiB: clamped to 1 MiB
    # each phase's clock-free cycles grow by its term (x 1 / (1 - kernel cycles per CPU-second / clock))
    assert w12 - c12 == pytest.approx(100.0 * 2e6, rel=0.05)
    assert w3 - c3 == pytest.approx(-50.0 * 2e6, rel=0.05)


def test_twin_key_fraction_and_false_matches():
    from tpch.model.workload import false_hash_matches, twin_key_fraction
    assert twin_key_fraction(6e8) == 0.0                                    # SF100: all keys < 2^32
    assert twin_key_fraction(6e9) == pytest.approx(2 * (6e9 - 2**32) / 6e9)  # SF1000: 0.568
    with pytest.raises(NotImplementedError):
        twin_key_fraction(2**33 + 1)
    catalog = {"orders": {"tuples": 1.5e9}}
    cond = "(lineitem.l_orderkey = orders.o_orderkey)"
    n = false_hash_matches(cond, ["orders"], 3.2e9, 0.75e9, catalog, 0.5)
    assert n == pytest.approx(3.2e9 * (0.5 * 0.5 + 0.75e9 / 2**32))       # twins + chance
    n = false_hash_matches("(orders.o_custkey = customer.c_custkey)", ["customer"], 1e9, 1e7, catalog, 0.5)
    assert n == pytest.approx(1e9 * 1e7 / 2**32)                           # chance only


def test_compare_flags_a_worse_condition():
    from tpch.model.evaluate import compare
    from types import SimpleNamespace as NS
    new = [{"dataset": "a", "role": "test", "pred": NS(time_s=110.0), "obs": {"time_s": 100.0}},
           {"dataset": "b", "role": "test", "pred": NS(time_s=101.0), "obs": {"time_s": 100.0}}]
    base = {"conditions": {"a": {"time_s": 101.0}, "b": {"time_s": 110.0}}}
    out = compare(new, base)
    assert "| a | test | 100.0 | 101.0 | +1.0% | 110.0 | +10.0% | **worse** |" in out
    assert out.endswith("1 better, 0 same (within 1.0 pt), 1 worse")
