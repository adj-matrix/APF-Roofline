"""E1 fix (M7): query-independent replicas of the I/O paths of one backend, parametrized only by the CPU
work per 128 KiB read. docs/ext/iostack.md, "Fix".

A query that scans a table is, to the I/O stack, a loop: read 128 KiB, spend c cycles on it. The replicas
run that loop on our own file (BIG_ROOT/bench/fio.dat, read-only) with c set by a busy wait of `think` µs:

  io:ENGINE:DIRECT:QD:THINK     replica_io.c (built into <out>): preadv of 16 x 8 KiB (psync, QD 1) or
                                io_uring READV with QD in flight consumed in order (uring), O_DIRECT or
                                buffered (page cache dropped first, one pass), THINK µs busy wait per read.
                                (fio 3.28 ignores runtime and SIGINT once thinktime is set, so not fio.)
                                psync direct   = sync / data       uring direct   = io_uring / data
                                psync buffered = sync / off        uring buffered = io_uring / off
  li-io:ENGINE:DIRECT:QD:THINK  as io:, but on lineitem's 1 GiB segment files of tpch_sf100 (read-only, one after
                                the other like PostgreSQL's segments; 86 GiB > RAM, so buffered reads reclaim)
  pool:DIRECT:K:THINK:CB        replica_pool.c on lineitem's segments: io_method = worker as PostgreSQL 18 runs it
                                (one shared queue, lowest idle worker woken, fan-out 2, own fds per worker),
                                K workers, 16 in flight, CB µs of completion callback (page verification, dU)
                                in the worker after each read; the backend thread busy-waits THINK µs per read
  poolfull:DIRECT:K:THINK:CB[:S]  as pool:, but like a PostgreSQL cold run (M8): the page cache is dropped
                                machine-wide first (sync; drop_caches = 3), a buffered cell reads lineitem once to
                                the end (86 GiB: node 0's 31 GiB fill after ~35 s, then reclaim for the rest),
                                a direct cell runs S s (default 100); counted over the whole run (perf -t until
                                the backend thread exits). Other cores: the job's turbostat and procmon logs,
                                cut at the cell's t0 / t1.
  readers:MODE:RATE[:K]         a busy loop (the backend in worker mode, which never enters the kernel for
                                I/O) while K reader processes (io_workers, default 3) read the file
                                sequentially, MODE direct or buffered (page cache dropped behind them), paced
                                to RATE MiB/s in total (if they can) = worker / data and worker / off.
  spin                          the busy loop alone (reference).

Per cell, on the thread that does the loop (perf stat -t, no inherit): cycles, user cycles, ref-cycles
-> clock, task-clock; per read: kernel cycles (the I/O path's CPU in the issuing thread), wall time;
all threads of the process (/proc sampling: io_uring's io-wq workers) and of the readers: CPU seconds;
turbostat over the cell: per-CPU busy / C6 on node 0 (other cores' activity).
Writes <out>/cells.jsonl.
  PYTHONPATH=src .venv/bin/python -m tpch.ext.iostack.replica --out results/bench/<ts>_iostack_m7
"""
from __future__ import annotations

import argparse
import json
import mmap
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

CHUNK = 128 * 1024
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
from hardware.clock_state import perf_counts, spin_until, turbostat_node0  # noqa: E402

EVENTS = "task-clock,cycles,cycles:u,ref-cycles,instructions,context-switches,cpu-migrations"
BIND = ["numactl", "--cpunodebind=0", "--membind=0"]
DEFAULT_CELLS = (
    ["spin"]
    + [f"io:psync:1:1:{t}" for t in (0, 60, 120, 200, 350, 700, 1000)]
    + [f"io:uring:1:16:{t}" for t in (60, 100, 140, 200, 1000)]
    + [f"io:uring:0:16:{t}" for t in (100, 140, 200, 1000)]
    + [f"io:psync:0:1:{t}" for t in (120, 250, 1000)]
    + [f"readers:{m}:{r}" for m in ("direct", "buffered") for r in (150, 500, 800, 1100)]
    + ["readers:direct:1100:1", "readers:buffered:1100:1"]
)


def thread_cpu(pid: int, seen: dict) -> None:
    """Accumulate utime + stime (s) of every thread of pid into seen[tid] = (comm, s); threads may exit later."""
    tck = os.sysconf("SC_CLK_TCK")
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except FileNotFoundError:
        return
    for tid in tids:
        try:
            raw = Path(f"/proc/{pid}/task/{tid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        f = raw[raw.rindex(")") + 2:].split()
        seen[int(tid)] = (comm, (int(f[11]) + int(f[12])) / tck)


def reader(mode: str, rate_mib: float, idx: int, dur: float, path: str) -> None:
    """One paced sequential reader (an I/O worker); its own region of the file."""
    flags = os.O_RDONLY | (os.O_DIRECT if mode == "direct" else 0)
    fd = os.open(path, flags)
    size = os.fstat(fd).st_size
    region = size // 3 // CHUNK * CHUNK
    start = idx * region
    off = start
    buf = mmap.mmap(-1, CHUNK)
    if mode != "direct":
        os.posix_fadvise(fd, start, region, os.POSIX_FADV_DONTNEED)
    period = CHUNK / (rate_mib * 2**20)
    t0 = time.perf_counter()
    t_end, n = t0 + dur, 0
    while time.perf_counter() < t_end:
        os.preadv(fd, [buf], off)
        n += 1
        off += CHUNK
        if mode != "direct" and (off - start) % (64 * 2**20) == 0:
            os.posix_fadvise(fd, start, off - start, os.POSIX_FADV_DONTNEED)
        if off >= start + region:
            if mode != "direct":
                os.posix_fadvise(fd, start, region, os.POSIX_FADV_DONTNEED)
            off = start
        wait = t0 + n * period - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
    os.close(fd)
    print(json.dumps({"reads": n}), flush=True)


def spinner(dur: float) -> None:
    spin_until(time.perf_counter() + dur)


def _perf(target: list[str], out: Path, meas: float) -> subprocess.Popen:
    return subprocess.Popen(["sudo", "-n", "env", "LC_ALL=C", "perf", "stat", "-x,", "--no-inherit", "-o", str(out),
                             "-e", EVENTS, *target, "--", "sleep", str(meas)])


def _turbostat(out: Path, meas: float) -> subprocess.Popen:
    return subprocess.Popen(["sudo", "-n", "turbostat", "--quiet", "--interval", str(meas - 1), "--num_iterations", "1",
                             "--out", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def io_cell(spec: str, dur: float, path: str, out: Path, tag: str, binary: Path) -> dict:
    kind, engine, direct, qd, think = spec.split(":")
    if kind == "li-io":
        path = "@" + str(out / "lineitem.list")
    run = dur if direct == "1" or kind == "li-io" else min(dur, 14)   # buffered fio.dat: < one pass (16 GiB)
    p = subprocess.Popen(BIND + [str(binary), path, engine, direct, qd, think, str(run)],
                         stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    meas = run - 3
    perf_out, ts_out = out / f"perf_{tag}.csv", out / f"turbostat_{tag}.txt"
    perf, ts = _perf(["-t", str(p.pid)], perf_out, meas), _turbostat(ts_out, meas)
    seen0: dict = {}
    thread_cpu(p.pid, seen0)
    seen: dict = dict(seen0)
    t_end = time.time() + meas
    while time.time() < t_end:
        thread_cpu(p.pid, seen)
        time.sleep(0.25)
    perf.wait()
    ts.wait()
    rj = json.loads(p.stdout.read().strip().splitlines()[-1])
    p.wait()
    c = perf_counts(perf_out)
    # CPU of the other threads of the process during the window (io_uring's io-wq workers)
    other = sum(s - seen0.get(t, (None, 0.0))[1] for t, (comm, s) in seen.items() if t != p.pid)
    comms = sorted({comm for t, (comm, _) in seen.items() if t != p.pid})
    rate = rj["reads"] / rj["elapsed_s"]
    return {"cycles": c["cycles"], "user_cycles": c["cycles:u"], "ref_cycles": c["ref-cycles"],
            "instructions": c.get("instructions", 0), "task_clock_s": c["task-clock"] / 1000,
            "cs": c.get("context-switches", 0), "migrations": c.get("cpu-migrations", 0), "meas_s": meas,
            "reads": rate * meas, "MiBps": rj["MiBps"], "wait_per_read_us": rj["wait_s"] / rj["reads"] * 1e6,
            "eof": rj["eof"], "other_threads_cpu_s": other, "other_threads": comms,
            "turbostat": turbostat_node0(ts_out)}


def pool_cell(spec: str, dur: float, out: Path, tag: str, binary: Path) -> dict:
    _, direct, k, think, cb = spec.split(":")
    p = subprocess.Popen(BIND + [str(binary), "@" + str(out / "lineitem.list"), direct, k, "16", think, cb, str(dur)],
                         stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    meas = dur - 3
    perf_out, ts_out = out / f"perf_{tag}.csv", out / f"turbostat_{tag}.txt"
    perf, ts = _perf(["-t", str(p.pid)], perf_out, meas), _turbostat(ts_out, meas)
    seen0: dict = {}
    thread_cpu(p.pid, seen0)
    seen: dict = dict(seen0)
    t_end = time.time() + meas
    while time.time() < t_end:
        thread_cpu(p.pid, seen)
        time.sleep(0.25)
    perf.wait()
    ts.wait()
    rj = json.loads(p.stdout.read().strip().splitlines()[-1])
    p.wait()
    c = perf_counts(perf_out)
    rate = rj["reads"] / rj["elapsed_s"]
    return {"cycles": c["cycles"], "user_cycles": c["cycles:u"], "ref_cycles": c["ref-cycles"],
            "task_clock_s": c["task-clock"] / 1000, "cs": c.get("context-switches", 0), "meas_s": meas,
            "reads": rate * meas, "MiBps": rj["MiBps"], "wait_per_read_us": rj["wait_s"] / rj["reads"] * 1e6,
            "eof": rj["eof"], "worker_reads": rj["worker_reads"],
            "other_threads_cpu_s": sum(s - seen0.get(t, (None, 0.0))[1] for t, (_, s) in seen.items() if t != p.pid),
            "turbostat": turbostat_node0(ts_out)}


def poolfull_cell(spec: str, out: Path, tag: str, binary: Path) -> dict:
    parts = spec.split(":")
    _, direct, k, think, cb = parts[:5]
    dur = parts[5] if len(parts) > 5 else ("100" if direct == "1" else "600")
    subprocess.run(["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"], check=True)
    time.sleep(2)
    t0 = time.time()
    p = subprocess.Popen(BIND + [str(binary), "@" + str(out / "lineitem.list"), direct, k, "16", think, cb, dur],
                         stdout=subprocess.PIPE, text=True)
    perf_out = out / f"perf_{tag}.csv"
    perf = subprocess.Popen(["sudo", "-n", "env", "LC_ALL=C", "perf", "stat", "-x,", "--no-inherit", "-o",
                             str(perf_out), "-e", EVENTS, "-t", str(p.pid)])
    seen: dict = {}
    while p.poll() is None:
        thread_cpu(p.pid, seen)
        time.sleep(0.25)
    t1 = time.time()
    perf.wait()
    rj = json.loads(p.stdout.read().strip().splitlines()[-1])
    c = perf_counts(perf_out)
    return {"cycles": c["cycles"], "user_cycles": c["cycles:u"], "ref_cycles": c["ref-cycles"],
            "task_clock_s": c["task-clock"] / 1000, "cs": c.get("context-switches", 0), "meas_s": rj["elapsed_s"],
            "reads": rj["reads"], "MiBps": rj["MiBps"], "wait_per_read_us": rj["wait_s"] / rj["reads"] * 1e6,
            "eof": rj["eof"], "worker_reads": rj["worker_reads"], "t0": t0, "t1": t1,
            "other_threads_cpu_s": sum(s for t, (_, s) in seen.items() if t != p.pid)}


def readers_cell(spec: str, dur: float, path: str, out: Path, tag: str) -> dict:
    _, mode, rate, *k = spec.split(":")
    k = int(k[0]) if k else 3
    me = [sys.executable, "-m", "tpch.ext.iostack.replica"]
    rs = [subprocess.Popen(BIND + me + ["--reader", mode, str(float(rate) / k), str(i), str(dur + 1), path],
                           stdout=subprocess.PIPE, text=True) for i in range(k)]
    time.sleep(0.5)
    sp = subprocess.Popen(BIND + me + ["--spinner", str(dur)])
    time.sleep(1.0)
    meas = dur - 3
    perf_out, ts_out = out / f"perf_{tag}.csv", out / f"turbostat_{tag}.txt"
    perf, ts = _perf(["-t", str(sp.pid)], perf_out, meas), _turbostat(ts_out, meas)
    seen0: dict = {}
    for r in rs:
        thread_cpu(r.pid, seen0)
    perf.wait()
    ts.wait()
    seen: dict = {}
    for r in rs:
        thread_cpu(r.pid, seen)
    reads = sum(json.loads(r.stdout.read().strip().splitlines()[-1])["reads"] for r in rs)
    for r in rs:
        r.wait()
    sp.wait()
    c = perf_counts(perf_out)
    return {"cycles": c["cycles"], "user_cycles": c["cycles:u"], "ref_cycles": c["ref-cycles"],
            "task_clock_s": c["task-clock"] / 1000, "meas_s": meas, "reader_reads": reads,
            "readers_MiBps": reads * CHUNK / 2**20 / (dur + 1),
            "readers_cpu_s": sum(s - seen0.get(t, (None, 0.0))[1] for t, (_, s) in seen.items()),
            "turbostat": turbostat_node0(ts_out)}


def spin_cell(dur: float, out: Path, tag: str) -> dict:
    sp = subprocess.Popen(BIND + [sys.executable, "-m", "tpch.ext.iostack.replica", "--spinner", str(dur)])
    time.sleep(1.0)
    meas = dur - 3
    perf_out, ts_out = out / f"perf_{tag}.csv", out / f"turbostat_{tag}.txt"
    perf, ts = _perf(["-t", str(sp.pid)], perf_out, meas), _turbostat(ts_out, meas)
    perf.wait()
    ts.wait()
    sp.wait()
    c = perf_counts(perf_out)
    return {"cycles": c["cycles"], "user_cycles": c["cycles:u"], "ref_cycles": c["ref-cycles"],
            "task_clock_s": c["task-clock"] / 1000, "meas_s": meas, "turbostat": turbostat_node0(ts_out)}


def _lineitem_list(cfg, dest: Path) -> None:
    import psycopg
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname=cfg.dbname(100), autocommit=True) as conn:
        rel, ddir = conn.execute("SELECT pg_relation_filepath('lineitem'), current_setting('data_directory')").fetchone()
    first = Path(ddir) / rel
    segs = [first] + sorted(first.parent.glob(first.name + ".*"), key=lambda p: int(p.suffix[1:]))
    dest.write_text("".join(f"{p}\n" for p in segs))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--cells", nargs="+", default=DEFAULT_CELLS)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--dur", type=float, default=25)
    ap.add_argument("--reader", nargs=5)
    ap.add_argument("--spinner", type=float)
    a = ap.parse_args()
    if a.reader:
        m, r, i, d, p = a.reader
        return reader(m, float(r), int(i), float(d), p)
    if a.spinner:
        return spinner(a.spinner)
    from ... import config, sysconf
    from ...shell import Shell
    cfg = config.load()
    path = str(Path(cfg.BIG_ROOT) / "bench" / "fio.dat")
    a.out.mkdir(parents=True, exist_ok=True)
    if any(c.startswith(("li-io:", "pool:", "poolfull:")) for c in a.cells):
        _lineitem_list(cfg, a.out / "lineitem.list")
    binary, pool_bin = a.out / "replica_io", a.out / "replica_pool"
    subprocess.run(["cc", "-O2", "-Wall", "-o", str(binary), str(Path(__file__).with_name("replica_io.c"))], check=True)
    subprocess.run(["cc", "-O2", "-Wall", "-pthread", "-o", str(pool_bin), str(Path(__file__).with_name("replica_pool.c"))],
                   check=True)
    seed = random.randrange(2**31)
    rng = random.Random(seed)
    order = []
    for r in range(a.reps):
        blk = list(a.cells)
        rng.shuffle(blk)
        order += [(c, r) for c in blk]
    (a.out / f"design_{int(time.time())}.json").write_text(json.dumps({"seed": seed, "order": order, "dur": a.dur, "file": path}))
    sh = Shell(False)
    with sysconf.applied(cfg, sh), open(a.out / "cells.jsonl", "a") as f:
        for spec, r in order:
            tag = f"{spec.replace(':', '_')}_r{r}"
            if spec.startswith(("io:", "li-io:")):
                res = io_cell(spec, a.dur, path, a.out, tag, binary)
            elif spec.startswith("pool:"):
                res = pool_cell(spec, a.dur, a.out, tag, pool_bin)
            elif spec.startswith("poolfull:"):
                res = poolfull_cell(spec, a.out, tag, pool_bin)
            elif spec.startswith("readers:"):
                res = readers_cell(spec, a.dur, path, a.out, tag)
            else:
                res = spin_cell(a.dur, a.out, tag)
            res = {"cell": spec, "rep": r, **res, "GHz": res["cycles"] / res["ref_cycles"] * 2.7,
                   "on_cpu": res["task_clock_s"] / res["meas_s"]}
            f.write(json.dumps(res) + "\n")
            f.flush()
            extra = ""
            if res.get("reads"):
                extra = (f" kcyc/read {(res['cycles'] - res['user_cycles']) / res['reads'] / 1e3:.1f}k"
                         f" MiB/s {res['MiBps']:.0f} other {res['other_threads_cpu_s']:.1f}s"
                         f" wait/read {res['wait_per_read_us']:.0f}us")
            elif "reader_reads" in res:
                extra = f" readers {res['readers_MiBps']:.0f} MiB/s cpu {res['readers_cpu_s']:.1f}s"
            print(f"{tag:28s} GHz {res['GHz']:.3f} on_cpu {res['on_cpu']:.3f}{extra}", flush=True)
            time.sleep(2)
    print("system settings (applied, then restored):\n  " + "\n  ".join(sh.log))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))   # unwind sysconf.applied (restore) when killed
    main()
