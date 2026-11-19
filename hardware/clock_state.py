#!/usr/bin/env python3
"""Clock and scan-wait microbenchmark for the unified model (docs/7-unified-model.md, Part A).

One worker process, bound to NUMA node 0 like PostgreSQL (numactl), runs one of these loads for DUR s:
  spin                busy loop (a warm, CPU-bound query)
  LOAD@pin            any load with the worker pinned to CPU 6 instead of all of node 0
  sleep:D[:L]         busy loop with an L ms sleep (default 0.3) inserted so that the on-CPU fraction is D
                      (a query that waits briefly and often, like Q3's temp-file reads), no I/O
  dread:D             busy loop with a synchronous random 128 KiB O_DIRECT read of READ_FILE as the idle
                      (a device wait instead of a timer: does the kind of idle matter, or only its length?)
  scan:X[:W]          sequential buffered 128 KiB preads of READ_FILE (our own file on sdb, read-only),
                      with X ms of busy work per 128 KiB (Q6 ≈ 0.2 ms, Q1 ≈ 1.0 ms of CPU per 128 KiB);
                      pages behind are dropped (posix_fadvise DONTNEED) so the page cache stays empty;
                      with W, also append W KiB in 8 KiB writes per 128 KiB read to a file in
                      <dir of READ_FILE>/spill (Q3's temp-file writes; deleted at the end of the cell)
  spin+wakers:K       busy loop while K other processes, one per other node-0 core, wake every 1 ms for
                      ~50 µs (other cores awake, no I/O, the worker itself never sleeps)
Measured per cell: the worker's cycles / ref-cycles (perf stat, sudo) → clock while on CPU; task-clock /
wall → on-CPU fraction; in scan cells, the time spent in pread calls that took > 0.5 ms; turbostat
(sudo) over the cell → per-CPU busy fraction and C-state residency on node 0.
Writes <out>/cells.jsonl (one line per cell) and the raw perf / turbostat outputs.
"""
import argparse, json, os, random, signal, subprocess, sys, time
from pathlib import Path

CHUNK = 128 * 1024
NODE0 = list(range(0, 32, 2))


def spin_until(t):
    while time.perf_counter() < t:
        pass


def worker(load, dur, read_file, out):
    """Runs in the bound child process; prints one JSON line with its own stats."""
    t_end = time.perf_counter() + dur
    st = {"load": load}
    if load == "spin" or load.startswith("spin+wakers"):
        spin_until(t_end)
    elif load.startswith("sleep:"):
        parts = load.split(":")
        d = float(parts[1])
        s = float(parts[2]) / 1000 if len(parts) > 2 else 0.0003
        w = s * d / (1 - d)
        idle_n, idle_s = 0, 0.0
        while time.perf_counter() < t_end:
            spin_until(time.perf_counter() + w)
            a = time.perf_counter()
            time.sleep(s)
            idle_n, idle_s = idle_n + 1, idle_s + time.perf_counter() - a
        st.update(idle_n=idle_n, idle_ms_mean=idle_s / max(idle_n, 1) * 1000)
    elif load.startswith("dread:"):                 # idle = a synchronous device read (O_DIRECT, random 128 KiB)
        import mmap
        d = float(load.split(":")[1])
        fd = os.open(read_file, os.O_RDONLY | os.O_DIRECT)
        nchunks = os.fstat(fd).st_size // CHUNK
        buf, rng = mmap.mmap(-1, CHUNK), random.Random(os.getpid())
        idle_n, idle_s = 0, 0.0
        while time.perf_counter() < t_end:
            a = time.perf_counter()
            os.preadv(fd, [buf], rng.randrange(nchunks) * CHUNK)
            idle_n, idle_s = idle_n + 1, idle_s + time.perf_counter() - a
            mean = idle_s / idle_n                   # busy work sized to the running mean read time -> on-CPU d
            spin_until(time.perf_counter() + mean * d / (1 - d))
        os.close(fd)
        st.update(idle_n=idle_n, idle_ms_mean=idle_s / max(idle_n, 1) * 1000)
    elif load.startswith("mem:"):                   # memory-bound, no I/O, no sleep (energy-efficient turbo?)
        import numpy as np
        rng = np.random.default_rng(1)
        if load == "mem:gather":                     # random 8-byte loads over 2 GiB
            a = np.arange(2**28, dtype=np.int64)
            idx = rng.integers(0, a.size, 2**22)
            while time.perf_counter() < t_end:
                a[idx].sum()
        else:                                        # mem:copy, 256 MiB memcpy (like copying pages from the page cache)
            a, b = np.ones(2**25), np.empty(2**25)
            while time.perf_counter() < t_end:
                np.copyto(b, a)
    elif load.startswith("scan:"):
        parts = load.split(":")
        x = float(parts[1]) / 1000
        wkib = int(parts[2]) if len(parts) > 2 else 0  # KiB appended (8 KiB writes) per 128 KiB read
        wfd = wbuf = None
        if wkib:
            wpath = Path(read_file).parent / "spill" / f"clock_w_{os.getpid()}.tmp"
            wpath.parent.mkdir(exist_ok=True)
            wfd, wbuf = os.open(wpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), os.urandom(8192)
        fd = os.open(read_file, os.O_RDONLY)
        size = os.fstat(fd).st_size
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        off, nbytes, slow_n, slow_s, read_s = 0, 0, 0, 0.0, 0.0
        while time.perf_counter() < t_end:
            a = time.perf_counter()
            n = len(os.pread(fd, CHUNK, off))
            b = time.perf_counter()
            read_s += b - a
            if b - a > 0.0005:
                slow_n += 1
                slow_s += b - a
            nbytes += n
            off += n
            if off % (256 * 2**20) == 0 or off >= size:
                os.posix_fadvise(fd, 0, off, os.POSIX_FADV_DONTNEED)
                if off >= size:
                    off = 0
            for _ in range(wkib // 8):
                os.write(wfd, wbuf)
            spin_until(time.perf_counter() + x)
        os.close(fd)
        if wfd is not None:
            os.close(wfd)
            wpath.unlink()
        st.update(MiB=nbytes / 2**20, read_call_s=read_s, slow_calls=slow_n, slow_s=slow_s)
    print(json.dumps(st), flush=True)


def waker(dur):
    t_end = time.perf_counter() + dur
    while time.perf_counter() < t_end:
        spin_until(time.perf_counter() + 0.00005)
        time.sleep(0.001)


def perf_counts(path):
    v = {}
    for line in Path(path).read_text().splitlines():
        f = line.split(",")
        if len(f) > 2 and f[0] and f[0][0].isdigit():
            v[f[2]] = float(f[0])
    return v


def turbostat_node0(path):
    """Mean per-CPU Busy% / Bzy_MHz / C-state residencies for node-0 CPUs from one turbostat interval."""
    lines = [l.split() for l in Path(path).read_text().splitlines() if l.strip()]
    hdr = next(l for l in lines if "CPU" in l and "Busy%" in l)
    ci = hdr.index("CPU")                            # rows after the first of a package omit the package columns
    rows = [dict(zip(hdr, l)) for l in lines if l is not hdr and len(l) > ci and l[ci].isdigit()]
    node0 = [r for r in rows if int(r["CPU"]) in NODE0]
    out = {"cpus": {}}
    for r in node0:
        out["cpus"][r["CPU"]] = {k: float(r[k]) for k in r if k not in ("Core", "CPU", "Package") and r[k].replace(".", "", 1).isdigit()}
    return out


def cell(load, dur, read_file, out_dir, tag):
    base, pinned = load.split("@")[0], load.endswith("@pin")
    me = [sys.executable, os.path.abspath(__file__), "--child", base, "--dur", str(dur)]
    if read_file:
        me += ["--read-file", str(read_file)]
    # LOAD@pin: the worker is pinned to one CPU of node 0 (no migrations) instead of the whole node
    bind = ["numactl", "--physcpubind=6", "--membind=0"] if pinned else ["numactl", "--cpunodebind=0", "--membind=0"]
    wakers = []
    if load.startswith("spin+wakers"):
        for c in NODE0[1:1 + int(load.split(":")[1])]:
            wakers.append(subprocess.Popen(["taskset", "-c", str(c), sys.executable, os.path.abspath(__file__),
                                            "--waker", "--dur", str(dur + 2)]))
        time.sleep(1)
    w = subprocess.Popen(bind + me, stdout=subprocess.PIPE, text=True)
    time.sleep(0.5)                                  # skip interpreter start-up
    perf_out, ts_out = out_dir / f"perf_{tag}.csv", out_dir / f"turbostat_{tag}.txt"
    meas = dur - 2
    perf = subprocess.Popen(["sudo", "-n", "env", "LC_ALL=C", "perf", "stat", "-x,", "-o", str(perf_out), "-e",
                             "task-clock,cycles,ref-cycles,instructions,LLC-load-misses,context-switches,cpu-migrations", "-p", str(w.pid), "--", "sleep", str(meas)])
    ts = subprocess.Popen(["sudo", "-n", "turbostat", "--quiet", "--interval", str(meas - 1), "--num_iterations", "1",
                           "--out", str(ts_out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    perf.wait()
    ts.wait()
    child = json.loads(w.stdout.read().strip().splitlines()[-1])
    w.wait()
    for p in wakers:
        p.wait()
    c = perf_counts(perf_out)
    res = {"tag": tag, "meas_s": meas, **child, "load": load,
           "GHz": c["cycles"] / c["ref-cycles"] * 2.7, "on_cpu": c["task-clock"] / 1000 / meas,
           "cs_per_s": c["context-switches"] / meas,
           "migrations_per_s": c.get("cpu-migrations", 0) / meas, "IPC": c.get("instructions", 0) / c["cycles"],
           "llc_miss_per_kinstr": c.get("LLC-load-misses", 0) / max(c.get("instructions", 1), 1) * 1000}
    try:
        res["turbostat"] = turbostat_node0(ts_out)
    except Exception as e:                           # keep the cell even if the format surprises us
        res["turbostat_error"] = repr(e)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    ap.add_argument("--loads", nargs="+", default=["spin", "sleep:0.95", "sleep:0.90", "scan:1.0", "scan:0.2", "scan:0.25:16",
                                                    "spin+wakers:1", "spin+wakers:3", "spin+wakers:7"])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--dur", type=float, default=60)
    ap.add_argument("--read-file", type=Path)
    ap.add_argument("--child")
    ap.add_argument("--waker", action="store_true")
    a = ap.parse_args()
    if a.child:
        return worker(a.child, a.dur, a.read_file, None)
    if a.waker:
        return waker(a.dur)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from tpch import config, sysconf
    from tpch.shell import Shell
    a.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random()
    seed = rng.randrange(2**31)
    rng.seed(seed)
    order = []
    for r in range(a.reps):
        blk = list(a.loads)
        rng.shuffle(blk)
        order += [(load, r) for load in blk]
    (a.out / "design.json").write_text(json.dumps({"seed": seed, "order": order, "dur": a.dur}))
    sh = Shell(False)
    with sysconf.applied(config.load(), sh), open(a.out / "cells.jsonl", "a") as f:
        for load, r in order:
            tag = f"{load.replace(':', '_').replace('+', '_').replace('@', '_')}_r{r}"
            res = cell(load, a.dur, a.read_file, a.out, tag)
            f.write(json.dumps(res) + "\n")
            f.flush()
            print(tag, f"GHz {res['GHz']:.3f} on_cpu {res['on_cpu']:.4f}",
                  {k: round(v, 2) for k, v in res.items() if k in ("idle_ms_mean", "MiB", "slow_calls", "slow_s", "cs_per_s", "migrations_per_s", "IPC", "llc_miss_per_kinstr")}, flush=True)
            time.sleep(3)
    print("system settings (applied, then restored):\n  " + "\n  ".join(sh.log))


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main()
