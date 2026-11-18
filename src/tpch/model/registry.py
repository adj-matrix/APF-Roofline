"""Which measurements the model may learn from, and which it is judged on.

Every dataset has a role:
  train   calibrate.py may read it
  test    only evaluate.py reads it; calibrate.py refuses (data.load(..., purpose="calibrate"))
  blind   like test, and its predictions are written (and committed) before it is run
Benchmarks (no TPC-H query) are listed in BENCH; they are always allowed for calibration.

Rules used to assign roles:
  * small scale factors (SF10) are training data: the model predicts large SFs from small ones;
  * at SF100, only Q3 at work_mem 64MB / 256MB of the v2 session trains the three wait / clock
    terms that no microbenchmark covers yet (temp read-back overlap, writeback-burst wait, clock vs
    on-CPU fraction); every other SF100 run is a test;
  * SF1000 is blind.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dataset:
    name: str
    role: str                                 # train | test | blind
    experiments: tuple[str, ...]              # experiment ids (results/experiments/<id>.json), globs allowed
    where: dict = field(default_factory=dict)  # run filters: sf, cache, mode, cpufreq (value or tuple of values)
    note: str = ""


D = Dataset
DATASETS = {d.name: d for d in [
    # --- SF10: work per tuple, kernel terms, spill cost -------------------------------------------------
    D("q6_sf10_warm", "train", ("20261114-194901_q6_main",), {"sf": 10, "cache": "warm"}),
    D("q6_sf10_cold", "train", ("20261114-194901_q6_main",), {"sf": 10, "cache": "cold"}),
    D("q6_sf10_plan", "train", ("20261114-203508_q6_main",), {"sf": 10, "mode": "analyze"}),
    D("q1_sf10_warm", "train", ("20261114-175007_q1_cpufreq",),
      {"sf": 10, "cache": "warm", "cpufreq": ("performance:0", "schedutil:0")},
      "turbo-on cells of the frequency experiment (stage-2 settings)"),
    D("q1_sf10_cold", "train", ("20261115-150501_q1_main",), {"sf": 10, "cache": "cold"},
      "interleaved with the Q1 x SF100 test runs, but SF10 on sda"),
    D("q1_sf10_plan", "train", ("20261115-161047_q1_main",), {"sf": 10, "mode": "analyze"}),
    D("q3_cal_1GB", "train", ("20261115-02*_q3_cal_wm1GB",), {"sf": 10, "mode": "plain"},
      "q3.sf100_shape (SF100's join order) at SF10, warm"),
    D("q3_cal_128MB", "train", ("20261115-02*_q3_cal_wm128MB",), {"sf": 10, "mode": "plain"}),
    D("q3_cal_64MB", "train", ("20261115-02*_q3_cal_wm64MB",), {"sf": 10, "mode": "plain"}),
    D("q3_cal_plan_1GB", "train", ("20261115-02*_q3_cal_wm1GB",), {"sf": 10, "mode": "analyze"}),
    D("q3_cal_plan_128MB", "train", ("20261115-02*_q3_cal_wm128MB",), {"sf": 10, "mode": "analyze"}),
    D("q3_cal_plan_64MB", "train", ("20261115-02*_q3_cal_wm64MB",), {"sf": 10, "mode": "analyze"}),
    # --- scan-only statements on sdb (not TPC-H queries): kernel cycles per page read from the device ----
    D("scan_sdb_orders_cold", "train", ("20261117-031127_scan_kpage",),
      {"sf": 100, "cache": "cold", "query": "scan.orders_heavy"},
      "orders (20 GiB, fits in RAM) with numeric work per tuple: CPU-bound like Q1"),
    D("scan_sdb_orders_warm", "train", ("20261117-031127_scan_kpage",),
      {"sf": 100, "cache": "warm", "query": "scan.orders_heavy"}),
    D("scan_sdb_lineitem_cold", "train", ("20261117-033338_scan_kpage",),
      {"sf": 100, "cache": "cold", "query": "scan.lineitem_heavy"},
      "lineitem (86 GiB > RAM): the same per page plus page reclaim"),
    # --- SF100, training: the three terms without a microbenchmark --------------------------------------
    D("q3_sf100_v2_64MB", "train", ("20261115-05*_q3_v2_wm64MB",), {"sf": 100, "mode": "plain"}),
    D("q3_sf100_v2_256MB", "train", ("20261115-05*_q3_v2_wm256MB",), {"sf": 100, "mode": "plain"}),
    D("q3_sf100_v2_plan_256MB", "train", ("20261115-05*_q3_v2_wm256MB",), {"sf": 100, "mode": "analyze"}),
    # --- after the Q3 x SF1000 blind test (2026-11-18): training data for its two misses -----------------
    # BIGINT order keys (SF > 357): the scale model on tpch_sf10 and tpch_sf10_bigkeys, interleaved
    *[D(f"q3_cal{k}_{wm}", "train", (f"20261118-11*_q3_calk_{k}_wm{wm}",), {"sf": 10, "mode": "plain"},
        "queries/q3/sf1000/cal_bigkeys.sh") for k in ("int", "big") for wm in ("64MB", "128MB", "1GB")],
    # SF1000's read-back regime at SF100 (512 / 256 batches), cycles per second (perf -I 1000)
    *[D(f"q3_sf100_rb_{wm}", "train", (f"20261118-1*_q3_rb_wm{wm}",), {"sf": 100, "mode": "plain"},
        "queries/q3/sf1000/cal_readback.sh") for wm in ("8MB", "16MB", "64MB")],
    *[D(f"q3_sf100_rb_plan_{wm}", "train", (f"20261118-1*_q3_rb_wm{wm}",), {"sf": 100, "mode": "analyze"})
      for wm in ("8MB", "16MB")],
    # hash twins of BIGINT order keys >= 2^32 (SF > 715): tpch_sf10_bigkeys vs tpch_sf10_twinkeys, interleaved
    *[D(f"q3_calt_{k}_{wm}", "train", (f"20261118-*_q3_calt_{k}_wm{wm}",), {"sf": 10, "mode": "plain"},
        "queries/q3/sf1000/cal_twinkeys.sh") for k in ("big", "twin") for wm in ("64MB", "128MB", "1GB")],
    *[D(f"q3_calt_plan_{k}_{wm}", "train", (f"20261118-*_q3_calt_{k}_wm{wm}",), {"sf": 10, "mode": "analyze"})
      for k in ("big", "twin") for wm in ("64MB", "128MB", "1GB")],
    # --- SF100, tests -------------------------------------------------------------------------------------
    D("q6_sf100", "test", ("20261114-194901_q6_main",), {"sf": 100, "cache": "cold"}),
    D("q6_sf100_plan", "test", ("20261114-203508_q6_main",), {"sf": 100, "cache": "cold", "mode": "analyze"}),
    D("q6_sf100_recheck", "test", ("20261115-1*_q6_u_recheck",), {"sf": 100}),
    D("q1_sf100", "test", ("20261115-150501_q1_main",), {"sf": 100}),
    D("q1_sf100_plan", "test", ("20261115-161047_q1_main",), {"sf": 100, "mode": "analyze"}),
    D("q3_sf100_64MB", "test", ("20261115-030315_q3_main",), {"sf": 100}, "the first Q3 x SF100 session"),
    D("q3_sf100_plan_64MB", "test", ("20261115-033932_q3_main",), {"sf": 100, "mode": "analyze"}),
    D("q3_sf100_128MB", "test", ("20261115-1*_q3_u_wm128MB",), {"sf": 100, "mode": "plain"}),
    D("q3_sf100_plan_128MB", "test", ("20261115-1*_q3_u_wm128MB",), {"sf": 100, "mode": "analyze"}),
    # --- SF1000: blind (predictions in model/predictions/, written before the runs) -----------------------
    D("q6_sf1000", "blind", ("*_q6_blind",), {"sf": 1000}, "prediction: model/predictions/q6_sf1000.json"),
    D("q6_sf100_control", "test", ("*_q6_blind",), {"sf": 100}, "same-session control of q6_sf1000"),
    D("q3_sf1000", "blind", ("*_q3_blind",), {"sf": 1000},
      "work_mem 64MB, enable_nestloop off; prediction: model/predictions/q3_sf1000.json"),
    D("q3_sf100_control", "test", ("*_q3_blind",), {"sf": 100}, "same-session control of q3_sf1000"),
    D("q3_sf1000_128MB", "blind", ("*_q3_blind128",), {"sf": 1000},
      "work_mem 128MB, enable_nestloop off; prediction: model/predictions/q3_sf1000_128MB.json (params 04da4632d83a)"),
    D("q3_sf100_control_128MB", "test", ("*_q3_blind128",), {"sf": 100}, "same-session control of q3_sf1000_128MB"),
]}

BENCH = {
    "temp_spill": ("20261115-045014_temp_spill", "20261115-174445_temp_spill", "20261118-003604_temp_spill",
                   "20261118-134033_temp_spill"),   # the last: N = 512 at x1 / x3 / x10 the volume (file size)
    "clock_state": ("20261115-163808_clock_state",),
    "location_big": ("20261114-172223_location",),
}

# How each query is calibrated: the small-SF scale model with the target's plan shape.
#   plan       dataset with the EXPLAIN ANALYZE of the scale model (actual rows to scale up)
#   cycles     {dataset: settings} plain runs of the scale model; several settings fit the spill slope.
#              User-mode cycles are taken from runs in the target's cache state where they exist (cold):
#              a cold page costs user-mode cycles too (Q6 SF10: +6 %), and the sign and size of that
#              effect differ between queries (Q1: -1 %), so it is not modelled as a per-page constant.
#              Q3 has warm calibration runs only.
CALIBRATION = {
    "q6": {"sf": 10, "plan": "q6_sf10_plan", "cycles": {"q6_sf10_cold": {}}},
    "q1": {"sf": 10, "plan": "q1_sf10_plan", "cycles": {"q1_sf10_cold": {}}},
    "q3": {"sf": 10, "plan": "q3_cal_plan_1GB",
           "cycles": {"q3_cal_1GB": {"work_mem": "1GB"}, "q3_cal_128MB": {"work_mem": "128MB"},
                      "q3_cal_64MB": {"work_mem": "64MB"}}},
}

# Test conditions evaluate.py replays: (dataset with the measured runs, dataset with a plan of the
# same condition, or None where there is no EXPLAIN ANALYZE (SF1000), settings).
TESTS = [
    ("q6_sf100", "q6_sf100_plan", {}),
    ("q6_sf100_recheck", "q6_sf100_plan", {}),
    ("q1_sf100", "q1_sf100_plan", {}),
    ("q3_sf100_64MB", "q3_sf100_plan_64MB", {"work_mem": "64MB"}),
    ("q3_sf100_128MB", "q3_sf100_plan_128MB", {"work_mem": "128MB"}),
    # training conditions, replayed to show the in-sample fit
    ("q3_sf100_v2_64MB", "q3_sf100_plan_64MB", {"work_mem": "64MB"}),
    ("q3_sf100_v2_256MB", "q3_sf100_v2_plan_256MB", {"work_mem": "256MB"}),
    ("q3_sf100_rb_8MB", "q3_sf100_rb_plan_8MB", {"work_mem": "8MB"}),
    ("q3_sf100_rb_16MB", "q3_sf100_rb_plan_16MB", {"work_mem": "16MB"}),
    ("q3_sf100_rb_64MB", "q3_sf100_plan_64MB", {"work_mem": "64MB"}),
    # SF1000: blind tests already run (their predictions are in model/predictions/); replayed with every
    # new parameter set, never used for calibration
    ("q6_sf1000", None, {}),
    ("q6_sf100_control", "q6_sf100_plan", {}),
    ("q3_sf1000", None, {"work_mem": "64MB", "enable_nestloop": "off"}),
    ("q3_sf100_control", "q3_sf100_plan_64MB", {"work_mem": "64MB", "enable_nestloop": "off"}),
    ("q3_sf1000_128MB", None, {"work_mem": "128MB", "enable_nestloop": "off"}),
    ("q3_sf100_control_128MB", "q3_sf100_plan_128MB", {"work_mem": "128MB", "enable_nestloop": "off"}),
]
