#!/usr/bin/env python3
"""Read-back with CPU work between reads: how much of the device latency a hash join's batch read-back
exposes (docs/8-model-framework.md, M4; replaces the fitted readback_overlap_gamma).

Observation that motivates it (Q3, ten conditions: SF100 at 16-512 batches, SF1000 at 256 and 512):
in phase 3 the backend's off-CPU time W3 is 0.32-0.48 x (device requests x r_await), even where its
CPU time per request c is 3x the latency L. A one-window-ahead readahead pipeline would hide all of
L once c > L (W3 = n max(0, L - c)), so the query does not behave like one.
This benchmark reproduces the read-back alone and varies c on a fixed layout:

  write   N inner + N outer batch files, 8 KiB blocks appended to random files at RATE MiB/s
          (as hardware/temp_spill.py: the layout ext4 gives interleaved writers)
  passes  for each CPU cost C (us of busy loop per 8 KiB block, shuffled; the first is repeated last
          as a drift check): drop the files from the page cache (fadvise), push them out of the
          controller cache (write + delete EVICT MiB), then read batch by batch, inner file then outer,
          sequential 8 KiB reads (BufFileRead), C us of spinning after each read.
Per pass: wall, process CPU time, off-CPU time W = wall - CPU, device requests n, await L, mean
request size, k = W / (n L), and the distribution of read() call times (bucketed count and sum): a
read either returns from the page cache (~10 us) or blocks; the histogram tells whether blocked reads
wait a whole L (synchronous reads) or a part of it (a read-ahead still in flight).
The layout is recorded after the write: extents per file (filefrag, FIEMAP on our own files) and the
number of physical discontinuities in read order (file starts included) -- the hypothesis from the smoke
test is that the off-CPU time is a few long waits (5-40 ms, seeks) at these discontinuities, which a
read-ahead one window ahead cannot hide; the first read of each file is timed separately.
Files only under DIR (our own volume, deleted at the end); no system settings changed.
"""
import argparse, json, os, random, re, statistics, subprocess, time
from pathlib import Path

from hardware.temp_spill import BLK, diskstat, write_side

BUCKETS_US = [50, 200, 500, 1000, 2000, 5000, 20000]   # upper edges; the last bucket is open


def drop(paths, evict_dir, evict_mib, tag):
    for p in paths:
        fd = os.open(p, os.O_RDONLY)
        os.fsync(fd)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)
    if evict_mib:                       # the RAID controller's cache holds ~1 GiB of recent writes
        ev, buf = evict_dir / f"{tag}_evict", os.urandom(2**20)
        fd = os.open(ev, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        for _ in range(int(evict_mib)):
            os.write(fd, buf)
        os.fsync(fd)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)
        ev.unlink()
    time.sleep(2)


EXTENT = re.compile(r"^\s*\d+:\s+(\d+)\.\.\s*(\d+):\s+(\d+)\.\.\s*(\d+):")


def layout(order):
    """Extents of the files in read order (4 KiB fs blocks): count, size, discontinuities between
    consecutive extents as the read-back visits them."""
    extents = []
    for p in order:
        out = subprocess.run(["filefrag", "-v", str(p)], capture_output=True, text=True, check=True).stdout
        extents.append([(int(m[3]), int(m[4])) for m in map(EXTENT.match, out.splitlines()) if m])
    def hist(files):
        edges = [1, 2, 4, 8, 16, 64]
        out = [0] * (len(edges) + 1)
        for f in files:
            for lo, hi in f:
                mib = (hi - lo + 1) * 4096 / 2**20
                out[next((i for i, e in enumerate(edges) if mib < e), len(edges))] += 1
        return out
    flat = [e for f in extents for e in f]
    jumps = [b[0] - a[1] - 1 for a, b in zip(flat, flat[1:])]
    sizes = [(b - a + 1) * 4096 / 2**20 for a, b in flat]
    return {"files": len(order), "extents": len(flat), "extent_MiB_median": statistics.median(sizes),
            "discontinuities": sum(1 for j in jumps if j != 0) + 1,
            "forward_jumps": sum(1 for j in jumps if j > 0), "backward_jumps": sum(1 for j in jumps if j < 0),
            "extent_MiB_hist_edges": [1, 2, 4, 8, 16, 64],
            "inner_hist": hist(extents[0::2]), "outer_hist": hist(extents[1::2]),
            "jump_GiB_median": statistics.median(abs(j) * 4096 / 2**30 for j in jumps if j) if any(jumps) else 0}


def read_pass(inner, outer, cpu_us, dev):
    spin = cpu_us / 1e6
    counts, sums = [0] * (len(BUCKETS_US) + 1), [0.0] * (len(BUCKETS_US) + 1)
    d0, w0, c0 = diskstat(dev), time.perf_counter(), time.process_time()
    total, first_n, first_s = 0, 0, 0.0
    for b in range(len(inner)):
        for p in (inner[b], outer[b]):
            fd = os.open(p, os.O_RDONLY)
            first = True
            while True:
                a = time.perf_counter()
                k = len(os.read(fd, BLK))
                e = time.perf_counter()
                us = (e - a) * 1e6
                if first:
                    first_n, first_s, first = first_n + 1, first_s + e - a, False
                i = next((j for j, edge in enumerate(BUCKETS_US) if us < edge), len(BUCKETS_US))
                counts[i] += 1
                sums[i] += e - a
                if not k:
                    break
                total += k
                if spin:
                    end = e + spin
                    while time.perf_counter() < end:
                        pass
            os.close(fd)
    wall, cpu, d1 = time.perf_counter() - w0, time.process_time() - c0, diskstat(dev)
    n = d1["r_ios"] - d0["r_ios"]
    await_ms = (d1["r_ms"] - d0["r_ms"]) / n if n else 0.0
    off = wall - cpu
    return {"cpu_us_per_block": cpu_us, "MiB": total / 2**20, "wall_s": wall, "cpu_s": cpu, "off_cpu_s": off,
            "requests": n, "await_ms": await_ms, "req_KiB": (d1["r_sec"] - d0["r_sec"]) * 512 / 1024 / n if n else 0,
            "busy_s": (d1["q_ms"] - d0["q_ms"]) / 1000,
            "cpu_ms_per_request": cpu / n * 1000 if n else 0, "k": off / (n * await_ms / 1000) if n and await_ms else 0,
            "first_reads": first_n, "first_read_s": first_s, "read_us_buckets": BUCKETS_US, "read_counts": counts, "read_sum_s": sums}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--dev", default="sdb")
    ap.add_argument("--batches", type=int, required=True, help="N: files per side")
    ap.add_argument("--inner", type=float, default=1200, help="MiB over the inner files")
    ap.add_argument("--outer", type=float, default=5600, help="MiB over the outer files")
    ap.add_argument("--rate", type=float, default=56, help="write rate, MiB/s")
    ap.add_argument("--cpu-us", default="0,20,40,60,90", help="busy-loop us per 8 KiB block, one pass each")
    ap.add_argument("--evict-mib", type=float, default=2048)
    a = ap.parse_args()

    a.dir.mkdir(parents=True, exist_ok=True)
    a.out.mkdir(parents=True, exist_ok=True)
    inner = [a.dir / f"{a.tag}_i{b:03d}" for b in range(a.batches)]
    outer = [a.dir / f"{a.tag}_o{b:03d}" for b in range(a.batches)]
    for p in inner + outer:
        p.unlink(missing_ok=True)
    rng = random.Random(1)
    costs = [float(x) for x in a.cpu_us.split(",")] if a.cpu_us else [0.0]   # --cpu-us '': layout only
    random.Random(a.tag).shuffle(costs)
    costs.append(costs[0])
    res = {"tag": a.tag, "batches": a.batches, "inner_MiB": a.inner, "outer_MiB": a.outer, "rate": a.rate,
           "passes": []}
    try:
        res["write_s"] = write_side(inner, a.inner, a.rate, rng) + write_side(outer, a.outer, a.rate, rng)
        drop(inner + outer, a.dir, 0, a.tag)          # fsync: extents are allocated at writeback
        res["layout"] = layout([p for b in range(a.batches) for p in (inner[b], outer[b])])
        print(json.dumps(res["layout"]), flush=True)
        for c in costs if a.cpu_us else []:
            drop(inner + outer, a.dir, a.evict_mib, a.tag)
            r = read_pass(inner, outer, c, a.dev)
            res["passes"].append(r)
            print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()
                              if not k.startswith("read_")}), flush=True)
    finally:
        for p in inner + outer:
            p.unlink(missing_ok=True)
    (a.out / f"result_{a.tag}.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
