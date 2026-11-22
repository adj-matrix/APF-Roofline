"""E1 predictions: one backend's wall time under io_method (sync / worker / io_uring) x io_direct (off / data).

Built from the frozen base prediction of the same query (tpch.model, read-only) plus two microbenchmark
curves the base does not use (results/bench/*_clock_m1, M1), see docs/ext/iostack.md:

  terms taken from the base:  U user cycles, K kernel cycles, K_dev = the part of K per device page (the
                              OS page cache: readahead, page management, copy into shared buffers),
                              table MiB, bw_big (QD1 O_DIRECT 128 KiB sequential, the location scan),
                              the start-up wait, the base clock curve (long idles)
  per cell:
    sync / buffered       = the base prediction
    io_uring / buffered   = the base prediction (buffered reads complete in the backend: the copy stays)
    worker / buffered     backend cycles U + K - K_dev; the copy runs in the I/O workers on other cores;
                          T = max(cpu, io) + start-up
    sync / direct         no readahead and no advice (seq scans declare READ_STREAM_SEQUENTIAL): each
                          128 KiB is a synchronous O_DIRECT read, so T = cpu + io; the backend idles ~0.12 ms
                          after every ~0.14 ms of work (Q6), the short-idle regime of M1
    io_uring / direct     16 x 128 KiB in flight, no copy: T = max(cpu, io) + start-up, backend CPU U + K - K_dev
    worker / direct       as io_uring / direct, the workers wake other cores
  clocks: a busy backend alone -> the base curve; with I/O workers awake -> M1's spin+wakers:3 (other
  cores waking every ~1 ms) as the point, the base clock as the upper end; sync / direct -> fixed point of
  the on-CPU share D = cpu / (cpu + io) on M1's short-idle curve (sleep L = 0.1 / 0.3 ms) as the point,
  the base long-idle curve (clock_readback) as the upper end.
Assumed zero (no measurement yet; the run measures them): the per-I/O cost of io_uring submission /
completion, of handing an I/O to a worker, and of an O_DIRECT read in the kernel.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from dataclasses import asdict, dataclass
from pathlib import Path

from ...config import Config, load as load_config
from ...model.evaluate import predict_condition
from ...model.params import ParamSet, interp

CELLS = [(m, d) for m in ("sync", "worker", "io_uring") for d in ("off", "data")]
M1_DIR = "20261119-183008_clock_m1"


@dataclass
class CellPrediction:
    io_method: str
    io_direct: str
    time_s: float
    time_range_s: tuple[float, float]
    cpu_s: float
    io_s: float
    backend_cycles: float
    clock_ghz: float
    clock_range_ghz: tuple[float, float]
    on_cpu_share: float
    note: str


def m1_curves(cfg: Config) -> dict:
    """From M1: the short-idle clock curve (sleep L <= 0.3 ms; on-CPU share -> GHz, medians per nominal D,
    with spin as D = 1) and the clock with three other cores waking (spin+wakers:3)."""
    cells = [json.loads(line) for line in
             (cfg.repo / cfg.RESULTS_DIR / "bench" / M1_DIR / "cells.jsonl").read_text().splitlines()]
    by_d: dict[float, list] = {}
    for c in cells:
        load = c["load"]
        if load.startswith("sleep:") and float(load.split(":")[2]) <= 0.3:
            by_d.setdefault(float(load.split(":")[1]), []).append(c)
        elif load == "spin":
            by_d.setdefault(1.0, []).append(c)
    short = sorted([st.median(c["on_cpu"] for c in v), st.median(c["GHz"] for c in v)] for v in by_d.values())
    wakers = st.median(c["GHz"] for c in cells if c["load"] == "spin+wakers:3")
    return {"short_idle_GHz_vs_on_cpu": short, "wakers3_GHz": wakers}


def _fixed_point(cycles: float, io_s: float, curve) -> tuple[float, float]:
    """Clock f and share D with D = (cycles / f) / (cycles / f + io_s), f = curve(D)."""
    f = interp(curve, 1.0, log_x=False)
    for _ in range(100):
        cpu = cycles / (f * 1e9)
        d = cpu / (cpu + io_s)
        f_new = interp(curve, d, log_x=False)
        if abs(f_new - f) < 1e-6:
            break
        f = 0.5 * (f + f_new)
    return f, d


def predict_cells(cfg: Config, params: ParamSet, query: str, sf: int) -> dict:
    base = predict_condition(cfg, params, query, sf, {})
    p, pv = base.parts, params.value
    m1 = m1_curves(cfg)
    u, k, k_dev = base.user_cycles, base.kernel_cycles, p["K_device_cycles"]
    io_s = base.workload["table_mib"] / pv("bw_big_MiBps")
    start = p["W_start_s"]
    no_copy = u + k - k_dev
    out = []
    for method, direct in CELLS:
        if direct == "off" and method in ("sync", "io_uring"):
            lo, hi = base.time_range_s
            out.append(CellPrediction(method, direct, base.time_s, (lo, hi), base.parts["cpu_s"], io_s,
                                      base.cycles, base.clock_ghz, (base.clock_ghz, base.clock_ghz),
                                      base.on_cpu_share,
                                      "base prediction" + ("" if method == "sync" else
                                                           "; buffered io_uring completes in the backend")))
            continue
        if method == "sync":                         # direct, synchronous: no overlap at all
            f, d = _fixed_point(no_copy, io_s, m1["short_idle_GHz_vs_on_cpu"])
            f_hi, _ = _fixed_point(no_copy, io_s, pv("clock_readback_GHz_vs_on_cpu"))
            cpu = no_copy / (f * 1e9)
            t = cpu + io_s + start
            t_lo = no_copy / (f_hi * 1e9) + io_s + start
            out.append(CellPrediction(method, direct, t, (t_lo, t), cpu, io_s, no_copy, f, (f, f_hi), d,
                                      "cpu + io serialized; clock: M1 short idles (point) .. base long idles"))
            continue
        alone = interp(pv("clock_GHz_vs_on_cpu"), 1.0, log_x=False)
        f_hi = alone
        f = m1["wakers3_GHz"] if method == "worker" else alone
        cpu, cpu_fast = no_copy / (f * 1e9), no_copy / (f_hi * 1e9)
        t, t_lo = max(cpu, io_s) + start, max(cpu_fast, io_s) + start
        out.append(CellPrediction(method, direct, t, (t_lo, t), cpu, io_s, no_copy, f, (f, f_hi),
                                  min(1.0, cpu / (t - start)),
                                  "max(cpu without the page-cache copy, io)"
                                  + ("; I/O workers wake other cores" if method == "worker" else "")))
    return {"query": query, "sf": sf, "params_digest": params.digest(), "m1": m1,
            "base": {"time_s": base.time_s, "U": u, "K": k, "K_device": k_dev, "io_s": io_s,
                     "clock_ghz": base.clock_ghz},
            "cells": [asdict(c) for c in out]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--query", required=True)
    ap.add_argument("--sf", type=int, default=100)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cfg = load_config()
    params = ParamSet.load(cfg.repo / "model" / "params.json")
    res = predict_cells(cfg, params, a.query, a.sf)
    for c in res["cells"]:
        print(f"{c['io_method']:9s} {c['io_direct']:5s} T {c['time_s']:7.1f} [{c['time_range_s'][0]:6.1f}, "
              f"{c['time_range_s'][1]:6.1f}]  cpu {c['cpu_s']:6.1f}  io {c['io_s']:5.1f}  "
              f"f {c['clock_ghz']:.3f} ({c['clock_range_ghz'][0]:.2f}-{c['clock_range_ghz'][1]:.2f})  "
              f"d {c['on_cpu_share']:.3f}  {c['note']}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, indent=1))
        print("prediction ->", a.out)


if __name__ == "__main__":
    main()
