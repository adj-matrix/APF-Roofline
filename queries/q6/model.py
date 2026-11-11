"""Q6 model v1: one serial Seq Scan over lineitem feeding a scalar Aggregate.

    io_s  = lineitem bytes / sequential read bandwidth          (cold cache)
    cpu_s = lineitem tuples * per-tuple executor cost           (scan + filter + agg)

Warm cache (table in the OS page cache) is modeled as io_s = 0: the page-cache -> shared_buffers
copy is inside cpu_ns_per_tuple when that parameter is measured from a warm run.
"""
from __future__ import annotations

from tpch.model.common import MiB, HwParams, Prediction, TableStats, combine


def predict_q6(lineitem: TableStats, hw: HwParams, cache: str = "cold", how: str = "max") -> Prediction:
    io_s = 0.0 if cache == "warm" else lineitem.bytes / (hw.seq_read_MiBps * MiB)
    cpu_s = lineitem.tuples * hw.cpu_ns_per_tuple * 1e-9
    assumptions = [
        "plan is a single non-parallel Seq Scan on lineitem + Aggregate (check plan.json)",
        "every lineitem page is read exactly once; no index, no buffer hits on a cold run",
        f"device sustains {hw.seq_read_MiBps:.0f} MiB/s for the scan's request size",
        f"executor cost {hw.cpu_ns_per_tuple:.0f} ns/tuple independent of table size",
        "aggregate output (1 row) and planning time negligible",
    ]
    if cache == "warm":
        assumptions.append("warm: whole table in OS page cache, no device reads")
    return combine({"io_s": io_s, "cpu_s": cpu_s}, assumptions, how)
