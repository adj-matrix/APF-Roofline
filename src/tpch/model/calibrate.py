"""Estimate every model parameter from benchmarks and training runs (registry.py).

Reads only BENCH directories and datasets with role 'train' (data.load enforces it). Writes a
ParamSet; `./tpch model calibrate` saves it to model/params.json, which predictions cite by digest.

Order matters: bench_params -> kernel_params -> query_params (per query) -> sf100_params; each step
uses parameters of the steps before it.
"""
from __future__ import annotations

import csv
import json
import statistics as st

from ..config import Config
from . import data, workload
from .params import Param, ParamSet, interp
from .plan import from_explain
from .registry import CALIBRATION

MiB = 1 << 20
PAGE = 8192


def _median(xs) -> float:
    return st.median(list(xs))


def _least_squares(xs, ys) -> tuple[float, float]:
    """(intercept, slope) of the least-squares line y = intercept + slope * x."""
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    slope = (sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
             / sum((x - mean_x) ** 2 for x in xs))
    return mean_y - slope * mean_x, slope


def _pool_non_decreasing(points):
    """Pool adjacent violators: [(x, y, weight)] -> [(x, y)] with y non-decreasing in x (weighted means)."""
    blocks = [[[x], y, w] for x, y, w in points]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            a, b = blocks[i], blocks[i + 1]
            blocks[i] = [a[0] + b[0], (a[1] * a[2] + b[1] * b[2]) / (a[2] + b[2]), a[2] + b[2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    return [(x, y) for xs, y, _ in blocks for x in xs]


def bench_params(cfg: Config, params: ParamSet) -> None:
    """Parameters measured by microbenchmarks that run no TPC-H query.

    Adds bw_big_MiBps, burst_s_per_GiB, scan_wait_frac (curve over CPU time per 128 KiB), and the
    read-back's extent model: readback_wait_per_extent_s, start_extents_vs_chunk_MiB, plus two constants of
    the system (writeback_expire_s from sysctl, mballoc_window_MiB from the ext4 source).
    """
    # sequential read bandwidth of the big volume where the SF100+ tables live
    location_dirs = data.bench_dirs(cfg, "location_big")
    rates = []
    for d in location_dirs:
        with open(d / "location.csv") as f:
            rates += [float(r["MiBps"]) for r in csv.DictReader(f) if r["target"].endswith(".lineitem")]
    params.add(Param("bw_big_MiBps", st.mean(rates), "MiB/s", "bench", [d.name for d in location_dirs],
                     "dd O_DIRECT 512 MiB reads over SF100 lineitem's files on sdb (hardware.md 6.1)"))

    # writeback bursts while batch files are written (hardware/temp_spill.py)
    bursts_per_gib = []
    spill_dirs = data.bench_dirs(cfg, "temp_spill")
    for d in spill_dirs:
        for f in sorted(d.glob("result_*.json")):
            result = json.loads(f.read_text())
            with open(d / f"timeline_{result['tag']}.csv") as t:
                writing = [x for x in csv.DictReader(t) if x["phase"] in ("write", "settle")]
            gib = (result["inner_MiB"] + result["outer_MiB"]) / 1024
            bursts_per_gib.append(sum(1 for x in writing if float(x["wMiBps"]) > 100) / gib)
    params.add(Param("burst_s_per_GiB", _median(bursts_per_gib), "s per GiB", "bench", [d.name for d in spill_dirs],
                     "seconds with > 100 MiB/s of writeback per GiB of temp written"))

    # the table scan's extra wait during those bursts (M5): temp_spill.py runs a 450 MiB/s reader (the scan)
    # while N batch files are written; hardware/burst_m5.sh adds the reader alone. Extra read-call time per
    # second of burst = (reader time at N - reader alone) / (burst_s_per_GiB x GiB written), over the x1
    # cells (6.64 GiB at 56 MiB/s, Q3 x SF100's rate); medians per N, made non-decreasing in N (pooling).
    m5_dirs = data.bench_dirs(cfg, "burst_m5")
    alone, at_n = [], {}
    for d in spill_dirs + m5_dirs:
        for f in sorted(d.glob("result_*.json")):
            result = json.loads(f.read_text())
            if "reader" not in result:
                continue
            if result.get("reader_only_s"):
                alone.append(result["reader"]["sum_s"])
            elif result["inner_MiB"] + result["outer_MiB"] == 6800:
                at_n.setdefault(result["batches"], []).append(result["reader"]["sum_s"])
    burst_s = params.value("burst_s_per_GiB") * 6800 / 1024
    base = _median(alone)
    raw = [(n, (_median(v) - base) / burst_s, len(v)) for n, v in sorted(at_n.items())]
    params.add(Param("burst_wait_per_burst_s", [[n, w] for n, w in _pool_non_decreasing(raw)],
                     "N batches -> s per burst-second", "bench", [d.name for d in spill_dirs + m5_dirs],
                     f"reader alone {base:.1f} s over 121 s; per N (raw, before pooling) "
                     + ", ".join(f"{n}: {w:.2f}" for n, w, _ in raw)
                     + ". The query fit it replaces (Q3 SF100, 2026-11-18): 16: 0.33, 64: 0.42, 256: 0.74, 512: 0.94"))

    # read-back of batch files: off-CPU time per extent jump (hardware/readback_cpu.py). The off-CPU time is
    # a set of 5-6 ms waits, ~4.4 per discontinuity, and barely depends on the CPU time between reads (-15 %
    # from 0.3 to 1.5 ms per request); the passes at 40-60 us per 8 KiB block are Q3's phase-3 range.
    readback_dirs = data.bench_dirs(cfg, "readback_cpu")
    per_extent, detail = [], []
    for d in readback_dirs:
        for f in sorted(d.glob("result_*.json")):
            result = json.loads(f.read_text())
            jumps = result["layout"]["discontinuities"]
            values = [p["off_cpu_s"] / jumps for p in result["passes"] if 40 <= p["cpu_us_per_block"] <= 60]
            per_extent += values
            detail.append(f"{result['tag']} ({jumps}): " + ", ".join(f"{v * 1e3:.1f}" for v in values))
    params.add(Param("readback_wait_per_extent_s", _median(per_extent), "s per extent", "bench",
                     [d.name for d in readback_dirs],
                     "off-CPU time of the read-back / discontinuities in read order, ms: " + "; ".join(detail)
                     + ". Q3 x SF100 temp files (queries/q3/sf1000/layout_tempfiles.sh, not used): 19-26 ms"))

    # extents of slowly written files: the first window's extents vs MiB per writeback (hardware/layout_rate.sh)
    window = 8.0
    params.add(Param("mballoc_window_MiB", window, "MiB", "system", [],
                     "ext4 mballoc ext4_mb_normalize_request: a request below 8 MiB is rounded up to an aligned "
                     "1/2/4/8 MiB window by file size; the rest stays preallocated for the file"))
    with open("/proc/sys/vm/dirty_expire_centisecs") as f:
        expiry = int(f.read()) / 100
    params.add(Param("writeback_expire_s", expiry, "s", "system", [],
                     "vm.dirty_expire_centisecs: dirty data older than this is written back (checked every 5 s)"))
    layout_dirs = data.bench_dirs(cfg, "layout_rate")
    start = [[window, 1.0]]                  # chunk = window: one extent per window, continuous with size / chunk
    for d in layout_dirs:
        for f in sorted(d.glob("result_*.json")):
            result = json.loads(f.read_text())
            files, size = result["batches"], result["outer_MiB"] / result["batches"]
            chunk = result["rate"] / files * expiry
            start.append([chunk, result["layout"]["extents"] / files - (size - window) / window])
    params.add(Param("start_extents_vs_chunk_MiB", sorted(start), "MiB per writeback -> extents", "bench",
                     [d.name for d in layout_dirs],
                     "extents of a file's first window: extents per file - (size - window) / window, 64 files of "
                     "40 MiB at 0.1 / 0.2 MiB/s; the point at the window by continuity"))

    # share of a CPU-bound buffered scan spent waiting for readahead, vs CPU time per 128 KiB
    clock_dirs = data.bench_dirs(cfg, "clock_state")
    wait_share = {}
    for d in clock_dirs:
        for line in open(d / "cells.jsonl"):
            cell = json.loads(line)
            if cell["load"] in ("scan:0.2", "scan:1.0"):
                cpu_ms = float(cell["load"].split(":")[1])
                wait_share.setdefault(cpu_ms, []).append(cell["slow_s"] / cell["meas_s"])
    params.add(Param("scan_wait_frac", [[x, _median(v)] for x, v in sorted(wait_share.items())],
                     "ms CPU per 128 KiB -> share", "bench", [d.name for d in clock_dirs],
                     "hardware/clock_state.py scan:X cells: time in pread calls > 0.5 ms / cell time"))


def kernel_params(cfg: Config, params: ParamSet) -> None:
    """Kernel cycles per CPU-second, per cached page and per page read from the device; start-up wait.

    Q6 and Q1 at SF10 warm read the same pages with different CPU time: their difference separates the
    per-CPU-second term from the per-page term. The per-device-page cost comes from scan-only
    statements on sdb (queries/scan/), not from TPC-H queries.
    """
    sf10 = workload.catalog(cfg, 10)
    lineitem_pages = sf10["lineitem"]["pages"]
    q6_warm, q1_warm = data.load(cfg, "q6_sf10_warm", "calibrate"), data.load(cfg, "q1_sf10_warm", "calibrate")
    q6_kernel, q6_cpu = _median(r.kernel_cycles for r in q6_warm), _median(r.cpu_s for r in q6_warm)
    q1_kernel, q1_cpu = _median(r.kernel_cycles for r in q1_warm), _median(r.cpu_s for r in q1_warm)
    per_cpu_s = (q1_kernel - q6_kernel) / (q1_cpu - q6_cpu)
    per_cached_page = (q6_kernel - per_cpu_s * q6_cpu) / lineitem_pages
    sources = ["q6_sf10_warm", "q1_sf10_warm"]
    params.add(Param("kernel_cycles_per_cpu_s", per_cpu_s, "cycles / CPU-second", "train", sources,
                     "kernel work proportional to running time (tick, scheduler); Q1 vs Q6 warm read the same pages"))
    params.add(Param("kernel_cycles_per_cached_page", per_cached_page, "cycles / 8 KiB page", "train", sources,
                     "pread of a page from the page cache into shared buffers"))

    q1_cold = data.load(cfg, "q1_sf10_cold", "calibrate")
    sf100 = workload.catalog(cfg, 100)

    def per_page(dataset: str, relation: str) -> float:
        return _median((r.kernel_cycles - per_cpu_s * r.cpu_s) / sf100[relation]["pages"]
                       for r in data.load(cfg, dataset, "calibrate"))

    sdb_orders_cold = per_page("scan_sdb_orders_cold", "orders")
    sdb_orders_warm = per_page("scan_sdb_orders_warm", "orders")
    sdb_lineitem_cold = per_page("scan_sdb_lineitem_cold", "lineitem")
    sda_q1 = _median((r.kernel_cycles - per_cpu_s * r.cpu_s) / lineitem_pages for r in q1_cold)
    params.add(Param("kernel_cycles_per_device_page", sdb_orders_cold, "cycles / 8 KiB page", "bench",
                     ["scan_sdb_orders_cold", "scan_sdb_orders_warm", "scan_sdb_lineitem_cold", "q1_sf10_cold"],
                     "page read from sdb (readahead, page-cache insert, copy), from a CPU-bound scan of SF100 "
                     f"orders (not a TPC-H query). Same scan warm: {sdb_orders_warm:.0f}; lineitem (> RAM, with "
                     f"reclaim): {sdb_lineitem_cold:.0f}; Q1 on sda at SF10: {sda_q1:.0f}. The queries imply "
                     "12.5k (Q6) - 12.8k (Q1): +-4 % depends on the code"))

    lineitem_mib = lineitem_pages * PAGE / MiB
    beyond_readahead = [r.off_cpu - interp(params.value("scan_wait_frac"), r.cpu_s / (lineitem_mib * 8) * 1000) * r.cpu_s
                        for r in q1_cold]
    params.add(Param("startup_wait_s", _median(beyond_readahead), "s", "train", ["q1_sf10_cold"],
                     "off-CPU time of a CPU-bound cold scan beyond its readahead waits"))


def query_params(cfg: Config, params: ParamSet, query: str) -> None:
    """Per-query parameters from the query's scale model (registry.CALIBRATION).

    Adds <query>.user_cycles_cal (user-mode cycles of the scale model without spill) and, for plans that
    spill: <query>.user_cycles_per_spilled_tuple (slope over the work_mem settings),
    <query>.temp_bytes_ratio (measured / formula temp bytes) and <query>.kernel_cycles_per_temp_page.
    """
    from .evaluate import server_defaults
    spec = CALIBRATION[query]
    sf = spec["sf"]
    cal_plan = data.load(cfg, spec["plan"], "calibrate")[0].plan
    catalog = workload.catalog(cfg, sf)

    settings_points = []          # one per work_mem setting
    for dataset, settings in spec["cycles"].items():
        runs = data.load(cfg, dataset, "calibrate")
        work = workload.build(query, cal_plan, cal_plan, sf, sf, catalog, {**server_defaults(cfg), **settings})
        join_temp_pages = sum(j.temp_bytes for j in work.joins) / PAGE
        measured_pages = None
        if join_temp_pages:   # the topmost Hash Join's (inclusive) temp blocks, from the same setting's plan
            plan = data.load(cfg, dataset.replace("_cal_", "_cal_plan_"), "calibrate")[0].plan
            measured_pages = max(n.raw.get("Temp Written Blocks", 0) for n in plan.walk() if n.type == "Hash Join")
        settings_points.append({"spilled": work.spilled_tuples, "temp_pages": join_temp_pages,
                                "measured_temp_pages": measured_pages, "runs": runs, "workload": work})

    no_spill = next(p for p in settings_points if p["spilled"] == 0)
    user_no_spill = _median(r.user_cycles for r in no_spill["runs"])
    params.add(Param(f"{query}.user_cycles_cal", user_no_spill, f"cycles at SF{sf}", "train", list(spec["cycles"]),
                     f"user-mode cycles of the scale model at SF{sf}, no spill"))
    spilling = [p for p in settings_points if p["spilled"] > 0]
    if not spilling:
        return

    intercept, per_tuple = _least_squares([p["spilled"] for p in settings_points],
                                          [_median(r.user_cycles for r in p["runs"]) for p in settings_points])
    params.add(Param(f"{query}.user_cycles_per_spilled_tuple", per_tuple, "cycles", "train", list(spec["cycles"]),
                     f"least squares over {len(settings_points)} work_mem settings; intercept {intercept:.4g} "
                     f"vs no-spill {user_no_spill:.4g}"))
    ratio = _median(p["measured_temp_pages"] / p["temp_pages"] for p in spilling)
    params.add(Param(f"{query}.temp_bytes_ratio", ratio, "measured / predicted", "train",
                     [spec["plan"]] + list(spec["cycles"]),
                     "the planner's widths vs the bytes PostgreSQL writes per spilled tuple"))

    per_cpu_s, per_cached_page = params.value("kernel_cycles_per_cpu_s"), params.value("kernel_cycles_per_cached_page")
    table_pages = sum(scan.pages for scan in no_spill["workload"].scans)

    def kernel_per_temp_page(point: dict) -> float:
        kernel = _median(r.kernel_cycles - per_cpu_s * r.cpu_s for r in point["runs"])
        return (kernel - per_cached_page * table_pages) / (point["temp_pages"] * ratio)

    params.add(Param(f"{query}.kernel_cycles_per_temp_page", _median(kernel_per_temp_page(p) for p in spilling),
                     "cycles / 8 KiB page", "train", list(spec["cycles"]),
                     "writing a temp page into the page cache and reading it back from there"))


def bigkeys_params(cfg: Config, params: ParamSet, query: str = "q3") -> None:
    """User-cycle factors of BIGINT order keys (SF > 357), from the scale model run on tpch_sf10 and
    tpch_sf10_bigkeys interleaved in one session (queries/q3/sf1000/cal_bigkeys.sh).

    Adds <query>.bigkeys_user_factor (no spill: BIGINT / INTEGER) and <query>.bigkeys_spill_factor (the
    same for the user cycles per spilled tuple: the cycles above the no-spill run, per setting).
    """
    def user(keys: str, wm: str) -> float:
        return _median(r.user_cycles for r in data.load(cfg, f"{query}_cal{keys}_{wm}", "calibrate"))

    sources = [f"{query}_cal{k}_{wm}" for k in ("int", "big") for wm in ("64MB", "128MB", "1GB")]
    no_spill = user("big", "1GB") / user("int", "1GB")
    spill = _median((user("big", wm) - user("big", "1GB")) / (user("int", wm) - user("int", "1GB"))
                    for wm in ("64MB", "128MB"))
    params.add(Param(f"{query}.bigkeys_user_factor", no_spill, "BIGINT / INTEGER", "train", sources,
                     "user-mode cycles of the scale model without spill (work_mem 1GB)"))
    params.add(Param(f"{query}.bigkeys_spill_factor", spill, "BIGINT / INTEGER", "train", sources,
                     "user-mode cycles above the no-spill run at 64MB and 128MB (cost per spilled tuple; "
                     "the spilled tuple is 4 B wider)"))


def false_match_params(cfg: Config, params: ParamSet, query: str = "q3") -> None:
    """User cycles per false hash match (a probe meeting a key that shares its 32-bit hash: order keys
    >= 2^32, SF > 715), from the scale model on tpch_sf10_bigkeys and tpch_sf10_twinkeys interleaved in one
    session (queries/q3/sf1000/cal_twinkeys.sh). The twin variant has SF1000's share of twinned keys.

    Adds user_cycles_per_false_hash_match: (user cycles with twins - without) / false matches, the median
    over the work_mem settings; the false matches are counted from the twin variant's EXPLAIN ANALYZE rows.
    """
    sf = CALIBRATION[query]["sf"]
    catalog = workload.catalog(cfg, sf)
    fraction = workload.twin_key_fraction(1000 * workload.ORDER_KEYS_PER_SF)
    per_match, detail = [], []
    for wm in ("64MB", "128MB", "1GB"):
        def user(keys: str) -> float:
            return _median(r.user_cycles for r in data.load(cfg, f"{query}_calt_{keys}_{wm}", "calibrate"))
        plan = data.load(cfg, f"{query}_calt_plan_twin_{wm}", "calibrate")[0].plan
        work = workload.build(query, plan, plan, sf, sf, catalog, {"work_mem": wm}, twin_fraction=fraction)
        matches = work.false_hash_matches
        per_match.append((user("twin") - user("big")) / matches)
        detail.append(f"{wm}: {matches / 1e6:.1f} M matches, {per_match[-1]:.0f}")
    params.add(Param("user_cycles_per_false_hash_match", _median(per_match), "cycles", "train",
                     [f"{query}_calt_{k}_{wm}" for k in ("big", "twin") for wm in ("64MB", "128MB", "1GB")],
                     "storing, deforming and comparing an inner tuple whose hash equals the probe's; " + "; ".join(detail)))


def readback_params(cfg: Config, params: ParamSet) -> None:
    """Spill terms of phases 1-2 and phase 3, from Q3 x SF100 at 8MB / 16MB / 64MB (512 / 256 / 64 batches),
    runs with cycles per second (queries/q3/sf1000/cal_readback.sh).

    At SF100 the number of batches N and the inner data per batch S move together (both set by work_mem),
    but they act in different phases (profiles of 8MB and 64MB, queries/q3/sf1000/profile_nbatch.sh):
    writing tuples into N batch buffers stalls on stores in phases 1-2 (grows with N), and joining a batch
    in its hash table misses the caches in phase 3 (grows with S). Relative to 64MB, per spilled tuple:

        spill_write_user_cycles_vs_nbatch   (phases 1-2 user cycles - those at 64MB) / spilled tuples
        spill_read_user_cycles_vs_batch_mib  phase-3 user cycles / spilled tuples - the same at 64MB

    so SF1000 / 64MB (N = 512, but S as SF100 / 64MB) takes each from the setting that shares it.

    Also adds readback_cycles_share (phase 3's share of the remaining base cycles, without either term and
    without the kernel cycles per CPU-second: one number) and clock_readback_GHz_vs_on_cpu (curve: phase-3
    clock vs phase-3 on-CPU share).
    """
    from .evaluate import workload_for
    conditions = {512: "8MB", 256: "16MB", 64: "64MB"}
    reference = "64MB"
    sources = [f"q3_sf100_rb_{wm}" for wm in conditions.values()]
    per_second_k = params.value("kernel_cycles_per_cpu_s")
    measured, clock_points = {}, []
    for nbatch, work_mem in conditions.items():
        work = workload_for(cfg, params, "q3", 100, {"work_mem": work_mem},
                            from_explain(workload.explain(cfg, "q3", 100, {"work_mem": work_mem})))
        biggest = max(work.joins, key=lambda j: j.temp_bytes)
        runs, run_clocks = [], []
        for run in data.load(cfg, f"q3_sf100_rb_{work_mem}", "calibrate"):
            rows = run.timeline
            split = len(rows) - run.phases["p3_len_s"]

            def total(column: str, part: list) -> float:
                return sum(r[column] for r in part)
            p12, p3 = rows[:split], rows[split:]
            cpu12, cpu3 = total("perf_task_ms", p12) / 1000, total("perf_task_ms", p3) / 1000
            runs.append({"C12": total("perf_cycles", p12), "C3": total("perf_cycles", p3),
                         "U12": total("perf_user_cycles", p12), "U3": total("perf_user_cycles", p3),
                         "cpu12": cpu12, "cpu3": cpu3})
            cycles3 = total("perf_cycles", p3)
            run_clocks.append((cpu3 / len(p3), cycles3 / cpu3 / 1e9))
        measured[work_mem] = {"nbatch": nbatch, "batch_mib": biggest.batch_inner_mib,
                              "spilled": work.spilled_tuples, "runs": runs,
                              **{k: _median(r[k] for r in runs) for k in ("U12", "U3")}}
        clock_points.append((_median(d for d, _ in run_clocks), _median(f for _, f in run_clocks)))

    ref = measured[reference]
    write, read = {}, {}
    for work_mem, m in measured.items():
        write[m["nbatch"]] = (m["U12"] - ref["U12"]) / m["spilled"]
        read[m["batch_mib"]] = m["U3"] / m["spilled"] - ref["U3"] / ref["spilled"]
    shares = []
    for work_mem, m in measured.items():
        extra_write, extra_read = write[m["nbatch"]] * m["spilled"], read[m["batch_mib"]] * m["spilled"]
        for r in m["runs"]:
            base3 = r["C3"] - per_second_k * r["cpu3"] - extra_read
            base12 = r["C12"] - per_second_k * r["cpu12"] - extra_write
            shares.append(base3 / (base12 + base3))
    detail = "; ".join(f"{wm}: N={m['nbatch']}, S={m['batch_mib']:.1f} MiB, U12 {m['U12'] / 1e9:.0f} G, "
                       f"U3 {m['U3'] / 1e9:.0f} G, spilled {m['spilled'] / 1e6:.0f} M" for wm, m in measured.items())
    params.add(Param("spill_write_user_cycles_vs_nbatch", [[n, v] for n, v in sorted(write.items())],
                     "N batches -> cycles / spilled tuple", "train", sources,
                     f"phases 1-2 user cycles above {reference}'s, per spilled tuple; clamped (N < 64: 0). {detail}"))
    params.add(Param("spill_read_user_cycles_vs_batch_mib", [[s_, v] for s_, v in sorted(read.items())],
                     "inner MiB per batch (planner widths) -> cycles / spilled tuple", "train", sources,
                     f"phase-3 user cycles per spilled tuple minus {reference}'s; clamped (larger batches: 0)"))
    params.add(Param("readback_cycles_share", _median(shares), "share of the base cycles", "train", sources,
                     "phase-3 cycles / cycles, both without the batch-count and batch-size terms and without the "
                     "kernel cycles per CPU-second; per run " + ", ".join(f"{x:.3f}" for x in shares)))
    params.add(Param("clock_readback_GHz_vs_on_cpu", [[d, f] for d, f in sorted(clock_points)],
                     "phase-3 on-CPU share -> GHz", "train", sources,
                     "medians per work_mem; clamped outside. Steeper than the scan phase: synchronous waits every "
                     "1-2 ms. Q3 x SF1000 (blind, not used): 0.66 -> 2.54 (turbostat)"))


def sf100_params(cfg: Config, params: ParamSet) -> None:
    """The terms without a microbenchmark, from Q3 x SF100.

    Adds page_cache_window_MiB and clock_GHz_vs_on_cpu (64MB and 256MB, N = 64 / 16, session v2).
    Target plans come from EXPLAIN (no execution). (burst_wait_per_burst_s was fitted here on all five
    conditions until 2026-11-19; it now comes from M5 in bench_params.)
    """
    from .evaluate import workload_for
    conditions = [(64, "q3_sf100_v2_64MB", "64MB"), (16, "q3_sf100_v2_256MB", "256MB")]
    v2 = ("q3_sf100_v2_64MB", "q3_sf100_v2_256MB")
    windows, clock_points = [], []
    for nbatch, dataset, work_mem in conditions:
        runs = data.load(cfg, dataset, "calibrate")
        work = workload_for(cfg, params, "q3", 100, {"work_mem": work_mem},
                            from_explain(workload.explain(cfg, "q3", 100, {"work_mem": work_mem})))
        biggest = max(work.joins, key=lambda j: j.temp_bytes)
        temp_mib, outer_temp_mib = work.temp_bytes / MiB, biggest.outer_temp_bytes / MiB
        for run in runs:
            # cache window: the outer batch files still cached = temp - temp read from the device
            windows.append((temp_mib - run.phases["p3_dev_read_MiB"]) * (biggest.outer_scan_mib + outer_temp_mib)
                           / outer_temp_mib)
            clock_points.append((run.on_cpu, run.ghz))

    params.add(Param("page_cache_window_MiB", _median(windows), "MiB", "train", list(v2),
                     "the most recent data the page cache still holds when the batches are read back; "
                     f"per run {', '.join(f'{x / 1024:.0f}' for x in windows)} GiB, against ~56 GiB of RAM free "
                     "for the cache (hypothesis: once-touched pages only get the inactive list)"))
    intercept, slope = _least_squares([d for d, _ in clock_points], [f for _, f in clock_points])
    lowest = min(d for d, _ in clock_points)
    params.add(Param("clock_GHz_vs_on_cpu", [[lowest, intercept + slope * lowest], [1.0, intercept + slope]],
                     "on-CPU share -> GHz", "train", list(v2),
                     f"least squares over {len(clock_points)} runs (slope {slope:.2f} GHz per unit); clamped "
                     f"outside [{lowest:.3f}, 1]; the microbenchmark (docs/7) explains the shape, not this slope"))


def calibrate(cfg: Config) -> ParamSet:
    """Estimate all parameters.

    Args:
        cfg: configuration (repo paths, database connection for catalogs and plans).

    Returns:
        A ParamSet with every parameter the cost model reads.
    """
    params = ParamSet()
    bench_params(cfg, params)
    kernel_params(cfg, params)
    for query in CALIBRATION:
        query_params(cfg, params, query)
    bigkeys_params(cfg, params)
    false_match_params(cfg, params)
    sf100_params(cfg, params)
    readback_params(cfg, params)
    return params
