"""E1 model v2: one backend's wall time per io_method x io_direct cell, with the terms v1 assumed away measured
query-independently (docs/ext/iostack.md, "Fix"). v1 (model.py) stays as registered.

Per cell, n = table MiB / 0.128 MiB reads (io_combine_limit 16 x 8 KiB):
  backend user cycles     U (base; trained on runs where the backend issues its own reads), minus n * dU when
                          the I/O workers issue every read (dU per read from SF10: sync / off vs worker / off)
  backend kernel cycles   buffered, in the backend (sync / off):  K (base)
                          O_DIRECT or io_uring submission in the backend:  K - K_device + n * k_path, k_path = the
                          replica's kernel cycles per read in the issuing thread for that path (M7)
                          worker:  K - K_device (the workers do the reads)
  backend time per read   (user + kernel) / f + off_cpu, with f and off_cpu (waiting) read from the replica of the
                          same path at the query's user cycles per read (the replica and the query then idle the same
                          share of the time, so the clock is the same)
  worker cells            the worker-pool replica (PostgreSQL's dispatch, K workers, page verification in the
                          workers) at the backend's user cycles per read U / n - dU: its clock and its waits
                          (off-CPU per read: the backend waiting for the workers, so a worker shortage shows)
  sync / off              the base prediction (unchanged); the replica value is printed as a check
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ...config import Config, load as load_config
from ...model.evaluate import predict_condition
from ...model.params import ParamSet, interp

READ_MIB = 0.125


@dataclass
class Cell:
    io_method: str
    io_direct: str
    extra: dict
    time_s: float
    cycles: float
    user_cycles: float
    kernel_cycles: float
    clock_ghz: float
    off_cpu_s: float
    note: str
    check: dict = field(default_factory=dict)


def latest_m7(cfg: Config) -> list[Path]:
    """M7 (25 s cells, our 16 GiB file), M7b (operating points, 120 s), M7c (lineitem's segments), M7d (worker pool)."""
    bench = cfg.repo / cfg.RESULTS_DIR / "bench"
    return [sorted(bench.glob(f"*_iostack_{m}"))[-1] for m in ("m7", "m7b", "m7c", "m7d") if list(bench.glob(f"*_iostack_{m}"))]


def load_replica(m7s: list[Path]) -> dict:
    """Per path ('psync'|'uring', '1'|'0'): points per think value (medians over reps) of user / kernel cycles
    per read, clock, off-CPU s per read, other-thread CPU s per read; readers (mode, K): rate -> clock,
    achieved rate; spin: the clock alone."""
    cells = [json.loads(line) for m7 in m7s for line in (m7 / "cells.jsonl").read_text().splitlines()]
    io: dict = {}
    li: dict = {}
    pool: dict = {}
    readers: dict = {}
    spin = []
    for c in cells:
        parts = c["cell"].split(":")
        if parts[0] in ("io", "li-io", "pool"):
            r = c["reads"]
            row = {"u": c["user_cycles"] / r, "k": (c["cycles"] - c["user_cycles"]) / r, "f": c["GHz"],
                   "t": c["meas_s"] / r, "off": (c["meas_s"] - c["task_clock_s"]) / r,
                   "other": c["other_threads_cpu_s"] / r, "MiBps": c["MiBps"]}
            if parts[0] == "pool":                     # pool:DIRECT:K:THINK:CB
                pool.setdefault((parts[1], int(parts[2])), {}).setdefault(parts[3], []).append(row)
            else:
                (li if parts[0] == "li-io" else io).setdefault((parts[1], parts[2]), {}).setdefault(
                    parts[4], []).append(row)
        elif parts[0] == "readers":
            k = int(parts[3]) if len(parts) > 3 else 3
            readers.setdefault((parts[1], k), {}).setdefault(float(parts[2]), []).append({
                "f": c["GHz"], "achieved": c["readers_MiBps"], "cpu": c["readers_cpu_s"], "meas": c["meas_s"]})
        else:
            spin.append(c["GHz"])
    med = lambda rows: {k: st.median(r[k] for r in rows) for k in rows[0]}
    by_u = lambda pts: sorted(([t, med(v)] for t, v in pts.items()), key=lambda x: x[1]["u"])
    paths = {}
    for p in set(io) | set(li):
        # buffered paths: lineitem's segments only (segment changes, reclaim, io-wq co-scheduling only show
        # there); direct paths: both file sets (no page cache, same device)
        pts = dict(li.get(p, {}))
        if p[1] == "1" or not pts:
            for t, v in io.get(p, {}).items():
                pts.setdefault(t, []).extend(v)
        paths[p] = by_u(pts)
    return {"io": paths, "pool": {p: by_u(v) for p, v in pool.items()},
            "readers": {p: sorted([rate, med(v)] for rate, v in pts.items()) for p, pts in readers.items()},
            "spin_GHz": st.median(spin) if spin else None, "dir": [str(m) for m in m7s]}


def _at(points: list, x: float, key: str, xkey: str = "u") -> float:
    return interp([[p[1][xkey], p[1][key]] for p in points], x, log_x=False)


def du_per_read(cfg: Config, query: str, sf: int = 10, since: str = "20261120") -> dict:
    """dU: the user cycles per read that move from the backend to the I/O workers in worker mode: PostgreSQL 18
    runs the read's completion callbacks (page verification with data checksums, bufmgr.c
    buffer_readv_complete_one -> PageIsVerified) in whichever process completes the I/O, the workers in
    worker mode (aio_callback.c:77). From the small-SF runs of the fix: mean user cycles of the cells where the
    backend completes its own reads (sync, io_uring; buffered or direct) - mean of the worker cells, per read."""
    runs = {}
    for d in sorted((cfg.repo / cfg.RESULTS_DIR / "runs").glob(f"{since}-*_{query}_sf{sf}_plain_cold_iostack_*")):
        s = json.loads((d / "summary.json").read_text())
        if s["label"].startswith("iostack_attr_"):
            continue
        runs.setdefault(s["label"][len("iostack_"):], []).append(s)
    u = {k: [r["perf_cycles"] * r["perf_user_cycles_frac"] for r in v] for k, v in runs.items()}
    n = st.median(r["dev_read_MiB"] for v in runs.values() for r in v) / READ_MIB
    self_u = st.mean(x for k, v in u.items() if not k.startswith("worker") for x in v)
    work_u = st.mean(x for k, v in u.items() if k.startswith("worker") for x in v)
    return {"dU": (self_u - work_u) / n, "n": n, "U_self": self_u, "U_worker": work_u,
            "cells": {k: [round(x / 1e9, 2) for x in v] for k, v in u.items()}}


def predict_cells(cfg: Config, params: ParamSet, query: str, sf: int, rep: dict, du: float,
                  cells: list[tuple[str, str, dict]]) -> dict:
    base = predict_condition(cfg, params, query, sf, {})
    p = base.parts
    table = base.workload["table_mib"]
    n = table / READ_MIB
    u, k, k_dev, start = base.user_cycles, base.kernel_cycles, p["K_device_cycles"], p["W_start_s"]
    k_rest = k - k_dev
    out = []
    for method, direct, extra in cells:
        if method in ("sync", "io_uring"):
            path = ("psync" if method == "sync" else "uring", "1" if direct == "data" else "0")
            pts = rep["io"][path]
            cu = u / n
            f, kp, off = _at(pts, cu, "f"), _at(pts, cu, "k"), _at(pts, cu, "off")
            kern = k if path == ("psync", "0") else k_rest + n * kp
            t = n * ((cu + kern / n) / (f * 1e9) + off) + start
            note = f"replica {path[0]} direct={path[1]}: f {f:.3f}, k/read {kp / 1e3:.1f}k, off/read {off * 1e6:.1f} us"
            cell = Cell(method, direct, extra, t, u + kern, u, kern, f, n * off, note,
                        {"other_cpu_s": n * _at(pts, cu, "other")})
            if (method, direct) == ("sync", "off"):     # the base stays the prediction; the replica is a check
                cell.check["replica_time_s"] = t
                cell.time_s, cell.clock_ghz = base.time_s, base.clock_ghz
                cell.note = "base prediction; " + note
            out.append(cell)
            continue
        kw = int(extra.get("io_workers", 3))
        pts = rep["pool"][("1" if direct == "data" else "0", kw)]
        cu = (u - n * du) / n                          # page verification runs in the workers
        f, off = _at(pts, cu, "f"), _at(pts, cu, "off")
        t = n * ((cu + k_rest / n) / (f * 1e9) + off) + start
        out.append(Cell(method, direct, extra, t, n * cu + k_rest, n * cu, k_rest, f, n * off,
                        f"pool replica direct={direct} x{kw}: f {f:.3f}, off/read {off * 1e6:.1f} us",
                        {"worker_cpu_s": n * _at(pts, cu, "other")}))
    return {"query": query, "sf": sf, "params_digest": params.digest(), "replica": rep["dir"], "dU_per_read": du,
            "base": {"time_s": base.time_s, "U": u, "K": k, "K_device": k_dev, "n_reads": n, "start_s": start},
            "cells": [asdict(c) for c in out]}


SEEN = {"q6": [(m, d, {}) for m in ("sync", "worker", "io_uring") for d in ("off", "data")],
        "q1": [("sync", "off", {}), ("sync", "data", {}), ("worker", "off", {}), ("io_uring", "data", {})]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--query", required=True)
    ap.add_argument("--sf", type=int, default=100)
    ap.add_argument("--cells", nargs="*", help="METHOD:DIRECT[:name=value,...]; default: the cells E1 ran")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cfg = load_config()
    params = ParamSet.load(cfg.repo / "model" / "params.json")
    rep = load_replica(latest_m7(cfg))
    du = du_per_read(cfg, a.query)
    cells = SEEN[a.query]
    if a.cells:
        cells = []
        for c in a.cells:
            m, d, *e = c.split(":")
            cells.append((m, d, dict(kv.split("=") for kv in e[0].split(",")) if e else {}))
    res = predict_cells(cfg, params, a.query, a.sf, rep, du["dU"], cells)
    res["dU_source"] = du
    for c in res["cells"]:
        ex = ",".join(f"{k}={v}" for k, v in c["extra"].items())
        print(f"{c['io_method']:9s} {c['io_direct']:5s} {ex:13s} T {c['time_s']:7.1f}  cyc {c['cycles'] / 1e9:7.1f}G "
              f"(U {c['user_cycles'] / 1e9:7.1f} K {c['kernel_cycles'] / 1e9:6.1f})  f {c['clock_ghz']:.3f}  "
              f"off {c['off_cpu_s']:5.1f}  {c['note']}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, indent=1))
        print("prediction ->", a.out)


if __name__ == "__main__":
    main()
