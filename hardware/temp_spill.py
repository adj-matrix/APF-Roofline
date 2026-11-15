#!/usr/bin/env python3
"""Temp-file spill microbenchmark: what a multi-batch hash join does to the device (docs/5-q3-sf100-prediction.md, Part D).

Mimics PostgreSQL's hybrid hash join on files under DIR (our own volume; never a raw device):
  write   for each 8 KiB block pick one of N batch files at random and append it (BufFile flushes 8 KiB
          blocks; tuples go to batches by hash), at RATE MiB/s: first the inner side (INNER MiB over N
          files), then the outer side (OUTER MiB over another N files). Meanwhile, optionally, a reader
          reads READ_FILE sequentially in 128 KiB preads at READ_RATE MiB/s through the page cache (the
          table scan), and the time each read call takes is recorded: does writeback disturb it?
  settle  wait SETTLE s, fsync, then drop the files from the page cache (posix_fadvise DONTNEED; no root)
  read    batch by batch: inner file b, then outer file b, sequential 8 KiB reads (BufFileRead), so that
          every byte comes from the device: the read-back rate as a function of N.
Per-second device counters (/sys/block/DEV/stat) go to <out>/timeline_<tag>.csv, results to <out>/result_<tag>.json.
"""
import argparse, json, os, random, threading, time
from pathlib import Path

BLK = 8192


def diskstat(dev):
    f = Path(f"/sys/block/{dev}/stat").read_text().split()
    return {"r_ios": int(f[0]), "r_sec": int(f[2]), "r_ms": int(f[3]),
            "w_ios": int(f[4]), "w_sec": int(f[6]), "w_ms": int(f[7]), "q_ms": int(f[10])}


class Sampler(threading.Thread):
    def __init__(self, dev, path):
        super().__init__(daemon=True)
        self.dev, self.path, self.stop, self.phase = dev, path, threading.Event(), "idle"
        self.reader_bytes = 0
        self.reader_call_s = 0.0

    def run(self):
        t0 = time.monotonic()
        prev, pb, pc = diskstat(self.dev), 0, 0.0
        with open(self.path, "w") as out:
            out.write("t_s,phase,rMiBps,r_per_s,r_await_ms,wMiBps,aqu,reader_MiBps,reader_wait_frac\n")
            while not self.stop.wait(1.0):
                cur = diskstat(self.dev)
                d = {k: cur[k] - prev[k] for k in cur}
                rb, rc = self.reader_bytes, self.reader_call_s
                out.write(f"{time.monotonic()-t0:.1f},{self.phase},{d['r_sec']*512/2**20:.1f},{d['r_ios']},"
                          f"{d['r_ms']/d['r_ios'] if d['r_ios'] else 0:.3f},{d['w_sec']*512/2**20:.1f},"
                          f"{d['q_ms']/1000:.2f},{(rb-pb)/2**20:.1f},{rc-pc:.3f}\n")
                out.flush()
                prev, pb, pc = cur, rb, rc


def reader(path, rate, stop, sampler, lat):
    """Sequential 128 KiB buffered reads at `rate` MiB/s, wrapping around; drops what it read behind it."""
    fd = os.open(path, os.O_RDONLY)
    size = os.fstat(fd).st_size
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    off, t0, done, chunk = 0, time.monotonic(), 0, 128 * 1024
    while not stop.is_set():
        a = time.monotonic()
        n = len(os.pread(fd, chunk, off))
        b = time.monotonic()
        lat.append(b - a)
        sampler.reader_bytes += n
        sampler.reader_call_s += b - a
        done += n
        off += n
        if off >= size:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            off = 0
        if off % (256 * 2**20) == 0:
            os.posix_fadvise(fd, 0, off, os.POSIX_FADV_DONTNEED)
        ahead = done / (rate * 2**20) - (time.monotonic() - t0)
        if ahead > 0:
            time.sleep(ahead)
    os.close(fd)


def write_side(paths, mib, rate, rng):
    fds = [os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600) for p in paths]
    buf = os.urandom(BLK)
    nblk, t0 = int(mib * 2**20 // BLK), time.monotonic()
    for i in range(nblk):
        os.write(fds[rng.randrange(len(fds))], buf)
        if i % 64 == 63:
            ahead = (i + 1) * BLK / (rate * 2**20) - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)
    for fd in fds:
        os.close(fd)
    return time.monotonic() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--dev", default="sdb")
    ap.add_argument("--batches", type=int, required=True, help="N: files per side")
    ap.add_argument("--inner", type=float, default=700, help="MiB over the inner files")
    ap.add_argument("--outer", type=float, default=3300, help="MiB over the outer files")
    ap.add_argument("--rate", type=float, default=56, help="write rate, MiB/s")
    ap.add_argument("--read-file", type=Path, help="table-scan stand-in, read while writing")
    ap.add_argument("--read-rate", type=float, default=450)
    ap.add_argument("--settle", type=float, default=5)
    ap.add_argument("--evict-mib", type=float, default=2048, help="write+delete this much before reading back")
    a = ap.parse_args()

    a.dir.mkdir(parents=True, exist_ok=True)
    a.out.mkdir(parents=True, exist_ok=True)
    inner = [a.dir / f"{a.tag}_i{b:03d}" for b in range(a.batches)]
    outer = [a.dir / f"{a.tag}_o{b:03d}" for b in range(a.batches)]
    for p in inner + outer:
        p.unlink(missing_ok=True)
    rng = random.Random(1)
    s = Sampler(a.dev, a.out / f"timeline_{a.tag}.csv")
    s.start()
    res = {"tag": a.tag, "batches": a.batches, "inner_MiB": a.inner, "outer_MiB": a.outer, "rate": a.rate}
    try:
        stop, lat = threading.Event(), []
        rt = None
        if a.read_file:
            rt = threading.Thread(target=reader, args=(a.read_file, a.read_rate, stop, s, lat), daemon=True)
            rt.start()
        s.phase = "write"
        res["write_s"] = write_side(inner, a.inner, a.rate, rng) + write_side(outer, a.outer, a.rate, rng)
        stop.set()
        if rt:
            rt.join()
            lat.sort()
            res["reader"] = {"calls": len(lat), "sum_s": sum(lat), "p50_ms": lat[len(lat)//2]*1e3,
                             "p99_ms": lat[int(len(lat)*.99)]*1e3, "max_ms": lat[-1]*1e3}
        s.phase = "settle"
        time.sleep(a.settle)
        for p in inner + outer:
            fd = os.open(p, os.O_RDONLY)
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)
        if a.evict_mib:                      # push our files out of the controller cache (~1 GiB)
            s.phase = "evict"
            ev, buf = a.dir / f"{a.tag}_evict", os.urandom(2**20)
            fd = os.open(ev, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            for _ in range(int(a.evict_mib)):
                os.write(fd, buf)
            os.fsync(fd)
            os.close(fd)
            ev.unlink()
        time.sleep(2)
        s.phase = "read"
        t0, total, per = time.monotonic(), 0, []
        for b in range(a.batches):
            for p in (inner[b], outer[b]):
                fd = os.open(p, os.O_RDONLY)
                f0, n = time.monotonic(), 0
                while True:
                    k = len(os.read(fd, BLK))
                    if not k:
                        break
                    n += k
                os.close(fd)
                per.append((p.name, n, time.monotonic() - f0))
                total += n
        res["read_s"] = time.monotonic() - t0
        res["read_MiB"] = total / 2**20
        res["read_MiBps"] = res["read_MiB"] / res["read_s"]
        res["files"] = per
        s.phase = "done"
        time.sleep(1.5)
    finally:
        s.stop.set()
        s.join()
        for p in inner + outer:
            p.unlink(missing_ok=True)
    (a.out / f"result_{a.tag}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "files"}))


if __name__ == "__main__":
    main()
