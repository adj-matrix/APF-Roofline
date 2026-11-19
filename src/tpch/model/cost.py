"""Time of a workload on this machine.

The model, in the symbols the reports use (docs/7), and the names this code uses for them:

    T = C / f + W                                     time_s = cycles / clock + wait_s

    C  cycles         cycles the backend spends on CPU, C = U + K
    U  user_cycles    user mode: the scale model's cycles x (SF / SF_cal)
                                 + cycles per spilled tuple x spilled tuples
                                 + per spilled tuple, by the number of batches N (writing to N batch
                                   buffers: store stalls), relative to N = 64
                                 + per spilled tuple, by the inner data per batch (joining a batch in a
                                   hash table that fits the caches or not), relative to SF100 / 64MB
                                 + per false hash match (BIGINT order keys >= 2^32 share hashes in pairs)
    K  kernel_cycles  kernel:    per table page read from the device
                                 + per temp page (write + read back through the page cache)
                                 + per temp page read back from the device instead of the cache
                                 + per CPU-second (timer tick, scheduler)
    f  clock_ghz      clock while on CPU, a function of the on-CPU share d = (C / f) / T, per phase:
                      phases 1-2 (table scans) and phase 3 (batch read-back) have their own curves
                      (f and d depend on each other: solved by fixed-point iteration)
    W  wait_s         off-CPU time: start-up
                                 + readahead waits of the table scans (or the I/O time beyond the CPU
                                   time, if the scan is I/O-bound)
                                 + phase 3: the batch files' extents read from the device x the wait per
                                   extent (hardware/readback_cpu.py: each jump to another extent costs a few
                                   mechanical accesses that a one-window readahead cannot hide, whatever
                                   the CPU time between reads); extents from the files' sizes and write
                                   rates (ext4: a file grows by rate x writeback expiry per allocation,
                                   allocations below mballoc's window are rounded up to it)
                                 + writeback-burst waits during the scans

Only cold runs on the big volume (SF >= 100, tables read from sdb) are modelled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .params import ParamSet, interp
from .workload import MiB, HashJoinSpill, Workload

PAGE = 8192


@dataclass
class Prediction:
    """A predicted run, every term kept so it can be compared with the measurement term by term.

    Attributes:
        query, sf, settings: the condition predicted.
        time_s: wall time T.
        time_range_s: (low, high) of T: cycles +-2 %, clock +-1 % (between sessions), waits +-30 %.
        cycles, user_cycles, kernel_cycles: C, U, K.
        clock_ghz: f, the clock while on CPU.
        on_cpu_share: d = CPU time / wall time.
        wait_s: W, the off-CPU time.
        parts: each term of U, K and W (cycles or seconds; the key says which).
        workload: the Workload predicted (Workload.as_dict()).
        params_digest: digest of the ParamSet used (model/params.json).
    """
    query: str
    sf: int
    settings: dict
    time_s: float
    time_range_s: tuple[float, float]
    cycles: float
    user_cycles: float
    kernel_cycles: float
    clock_ghz: float
    on_cpu_share: float
    wait_s: float
    parts: dict = field(default_factory=dict)
    workload: dict = field(default_factory=dict)
    params_digest: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def temp_read_from_device_mib(workload: Workload, cache_window_mib: float) -> float:
    """MiB of the hash joins' batch files read back from the device rather than the page cache.

    LRU approximation: when the batches are read back, the page cache holds the most recent
    `cache_window_mib` of what the query read and wrote (a trained window, smaller than the free RAM).
    The outer side's batch files were written while the outer tables were scanned, so they are
    outer_temp / (outer_scan + outer_temp) of that window; the inner side's files were written before
    the outer scan and have left the cache once the window is shorter than the scan.

    Args:
        workload: the workload; only its hash joins are used.
        cache_window_mib: the page-cache window (param page_cache_window_MiB).

    Returns:
        MiB read from the device, summed over the joins.
    """
    from_device = 0.0
    for join in workload.joins:
        temp = join.temp_bytes / MiB
        outer_temp = join.outer_temp_bytes / MiB
        stream = join.outer_scan_mib + outer_temp          # what passed through the cache meanwhile
        cached = outer_temp if stream <= cache_window_mib else cache_window_mib * outer_temp / stream
        from_device += temp - cached
    return from_device


def extents_per_file(size_mib: float, chunk_mib: float, window_mib: float, start_curve: list) -> float:
    """On-disk extents of one batch file (hardware/layout_rate.sh, queries/q3/sf1000/layout_tempfiles.sh).

    Delayed allocation places a file's dirty data when writeback takes it: about every expiry period,
    `chunk_mib` = write rate x expiry (the whole file if it is written faster). Interleaved writers make
    each allocation an extent of its own, except that ext4's mballoc rounds a request below its window
    (8 MiB) up to an aligned window and keeps the rest for the file's next writes: slow files get
    window-sized extents, after a few smaller ones at the start while the file is small
    (start_curve: extents of the first window vs chunk, measured).

    Args:
        size_mib: the file's final size.
        chunk_mib: MiB the file grows by between two writebacks (at most its size).
        window_mib: mballoc's largest normalisation window.
        start_curve: [[chunk MiB, extents in the first window], ...].
    """
    if chunk_mib >= window_mib:
        return max(1.0, size_mib / chunk_mib)
    start = interp(start_curve, chunk_mib, log_x=False)
    if size_mib <= window_mib:
        return max(1.0, start * size_mib / window_mib)
    return start + (size_mib - window_mib) / window_mib


def batch_file_extents(join: HashJoinSpill, params: ParamSet, inner_write_s: float, outer_write_s: float) -> float:
    """Extents of a join's batch files: N - 1 inner and N - 1 outer files (batch 0 stays in memory),
    the inner side written over `inner_write_s`, the outer side over `outer_write_s`."""
    files = join.nbatch - 1
    expiry, window, start = (params.value("writeback_expire_s"), params.value("mballoc_window_MiB"),
                             params.value("start_extents_vs_chunk_MiB"))
    total = 0.0
    for side_bytes, seconds in ((join.temp_bytes - join.outer_temp_bytes, inner_write_s),
                                (join.outer_temp_bytes, outer_write_s)):
        size = side_bytes / MiB / files
        chunk = min(size, size / max(seconds, 1e-9) * expiry)
        total += files * extents_per_file(size, chunk, window, start)
    return total


def predict(workload: Workload, params: ParamSet, iterations: int = 50) -> Prediction:
    """Predict the time of a workload, term by term.

    A spilling plan runs in two phases with different clocks:
      phases 1-2  the table scans; the joins write their batch files (scan clock, by on-CPU share)
      phase 3     the batch files are read back and joined (read-back clock: there the backend waits
                  synchronously every 1-2 ms, and the clock falls much faster with the on-CPU share)
    Phase 3's time is max(cpu, stream) + extents x wait per extent: the CPU time or the transfer at the
    volume's sequential rate, whichever is longer, plus the jumps between extents, which a readahead one
    window ahead does not hide (hardware/readback_cpu.py: the wait does not shrink with CPU time). A plan that does not spill has
    phases 1-2 only (the single-phase model).

    Args:
        workload: what the query does at the target SF (workload.build).
        params: calibrated parameters (calibrate.calibrate or model/params.json).
        iterations: limit of the fixed-point iteration over clocks and on-CPU shares.

    Returns:
        The Prediction, with every term in `parts`.
    """
    q = workload.query
    pv = params.value
    window = pv("page_cache_window_MiB")

    # --- cycles that do not depend on the clock --------------------------------------------------
    user_scale = pv(f"{q}.user_cycles_cal") * workload.scale
    user_spill = pv(f"{q}.user_cycles_per_spilled_tuple") * workload.spilled_tuples if workload.spilled_tuples else 0.0
    if workload.bigint_keys and f"{q}.bigkeys_user_factor" in params:
        user_scale *= pv(f"{q}.bigkeys_user_factor")
        user_spill *= pv(f"{q}.bigkeys_spill_factor")
    spilling = [j for j in workload.joins if j.temp_bytes]
    nbatch = max((j.nbatch for j in spilling), default=1)
    user_write = user_read = 0.0
    if spilling and "spill_write_user_cycles_vs_nbatch" in params:
        biggest = max(spilling, key=lambda j: j.temp_bytes)    # the read-back term by its batch size
        user_write = interp(pv("spill_write_user_cycles_vs_nbatch"), nbatch) * workload.spilled_tuples
        user_read = interp(pv("spill_read_user_cycles_vs_batch_mib"), biggest.batch_inner_mib) * workload.spilled_tuples
    user_twin = (pv("user_cycles_per_false_hash_match") * workload.false_hash_matches
                 if "user_cycles_per_false_hash_match" in params else 0.0)
    user = user_scale + user_spill + user_write + user_read + user_twin

    temp_from_device_mib = temp_read_from_device_mib(workload, window) if workload.temp_pages else 0.0
    k_device, k_cached, k_per_cpu_s = (pv("kernel_cycles_per_device_page"), pv("kernel_cycles_per_cached_page"),
                                       pv("kernel_cycles_per_cpu_s"))
    kernel_tables = k_device * workload.table_pages
    kernel_temp = 0.0
    if workload.temp_pages:
        kernel_temp = (pv(f"{q}.kernel_cycles_per_temp_page") * workload.temp_pages
                       + (k_device - k_cached) * temp_from_device_mib * MiB / PAGE)
    base_cycles = user_scale + user_spill + kernel_tables + kernel_temp

    # --- phase 3: its cycles, device time of the read-back -----------------------------------------
    # the base cycles split at a fixed share (SF100: the same at 512, 256 and 64 batches once the batch-count
    # and batch-size terms are taken out); the batch-size term is phase 3's, the batch-count term phases
    # 1-2's; a false match happens where its probe is joined: batch 0 during the scan, the rest in phase 3
    phase3_share = pv("readback_cycles_share") if spilling and "readback_cycles_share" in params else 0.0
    twin3 = user_twin * (1 - 1 / nbatch) if phase3_share else 0.0
    free12 = (1 - phase3_share) * base_cycles + user_write + (user_twin - twin3)
    free3 = phase3_share * base_cycles + user_read + twin3
    # per join: MiB read back from the device, and the share of its extents that the device serves
    device_mib = {id(j): temp_read_from_device_mib(_single_join(workload, j), window) for j in spilling}
    readback_stream_s = sum(device_mib.values()) / pv("bw_big_MiBps")
    wait_per_extent = pv("readback_wait_per_extent_s") if phase3_share else 0.0

    # --- waits of phases 1-2 that do not depend on the clock ---------------------------------------
    wait_burst = sum(interp(pv("burst_wait_per_burst_s"), j.nbatch) * pv("burst_s_per_GiB") * j.temp_bytes / 2**30
                     for j in spilling)
    table_io_s = workload.table_mib / pv("bw_big_MiBps")

    # --- clocks and on-CPU shares: fixed point ---------------------------------------------------
    scan_curve = pv("clock_GHz_vs_on_cpu")
    readback_curve = pv("clock_readback_GHz_vs_on_cpu") if phase3_share else None
    clock12 = interp(scan_curve, 1.0, log_x=False)
    clock3 = interp(readback_curve, 1.0, log_x=False) if readback_curve else clock12
    for _ in range(iterations):
        # kernel cycles per CPU-second are part of each phase's cycles: C = clock-free + b x cpu
        cycles12 = free12 / (1 - k_per_cpu_s / (clock12 * 1e9))
        cycles3 = free3 / (1 - k_per_cpu_s / (clock3 * 1e9))
        cycles = cycles12 + cycles3
        cpu12, cpu3 = cycles12 / (clock12 * 1e9), cycles3 / (clock3 * 1e9)
        cpu_ms_per_128k = cpu12 / (workload.table_mib * 8) * 1000
        wait_scan = max(0.0, table_io_s - cpu12) + interp(pv("scan_wait_frac"), cpu_ms_per_128k) * cpu12
        wait12 = pv("startup_wait_s") + wait_scan + wait_burst
        time12 = cpu12 + wait12
        extents = 0.0
        if phase3_share:
            # the outer side's files are written while the outer side's tables are scanned, the inner's before
            for j in spilling:
                outer_s = time12 * min(1.0, j.outer_scan_mib / workload.table_mib) if workload.table_mib else time12
                extents += (batch_file_extents(j, params, time12 - outer_s, outer_s)
                            * device_mib[id(j)] / (j.temp_bytes / MiB))
        wait_jumps = extents * wait_per_extent
        time3 = max(readback_stream_s, cpu3) + wait_jumps if phase3_share else 0.0
        new12 = interp(scan_curve, cpu12 / time12, log_x=False)
        new3 = interp(readback_curve, cpu3 / time3, log_x=False) if phase3_share else new12
        if abs(new12 - clock12) < 1e-6 and abs(new3 - clock3) < 1e-6:
            break
        # damped: the read-back clock curve is steep, an undamped step can oscillate
        clock12, clock3 = new12, 0.5 * (clock3 + new3)

    cpu_s = cpu12 + cpu3
    time_s = time12 + time3
    wait = time_s - cpu_s
    clock = cycles / (cpu_s * 1e9)
    low = cycles * 0.98 / (clock * 1.01e9) + 0.7 * wait
    high = cycles * 1.02 / (clock * 0.99e9) + 1.3 * wait
    parts = {"U_scale_cycles": user_scale, "U_spill_cycles": user_spill, "U_write_nbatch_cycles": user_write,
             "U_read_batch_size_cycles": user_read, "U_false_match_cycles": user_twin,
             "K_device_cycles": kernel_tables, "K_temp_cycles": kernel_temp,
             "K_time_cycles": cycles - free12 - free3, "cpu_s": cpu_s, "T_io_s": table_io_s,
             "x_ms_per_128KiB": cpu_ms_per_128k, "W_start_s": pv("startup_wait_s"), "W_scan_s": wait_scan,
             "W_burst_s": wait_burst, "W_temp_s": time3 - cpu3,
             "phase12_s": time12, "phase12_cpu_s": cpu12, "phase12_cycles": cycles12, "phase3_cycles": cycles3, "phase12_clock_GHz": clock12,
             "phase3_s": time3, "phase3_cpu_s": cpu3, "phase3_clock_GHz": clock3 if phase3_share else 0.0,
             "phase3_stream_s": readback_stream_s, "phase3_extents": extents, "W_jump_s": wait_jumps,
             "temp_dev_MiB": temp_from_device_mib,
             "temp_MiB": workload.temp_bytes / MiB}
    return Prediction(query=q, sf=workload.sf, settings=workload.settings, time_s=time_s, time_range_s=(low, high),
                      cycles=cycles, user_cycles=user, kernel_cycles=cycles - user, clock_ghz=clock,
                      on_cpu_share=cpu_s / time_s, wait_s=wait, parts=parts, workload=workload.as_dict(),
                      params_digest=params.digest())


def _single_join(workload: Workload, join: HashJoinSpill) -> Workload:
    """The workload with only one of its hash joins (for per-join terms)."""
    return Workload(query=workload.query, sf=workload.sf, sf_cal=workload.sf_cal, settings=workload.settings,
                    scale=workload.scale, scans=workload.scans, joins=[join])
