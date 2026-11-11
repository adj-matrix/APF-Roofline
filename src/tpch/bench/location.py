"""Sequential read bandwidth vs physical position on the volume: a property of the device.

An HDD passes more sectors per revolution on its outer tracks (low LBA), so "the disk's
bandwidth" depends on where a file lies. On kugenuma06 the root LV is the first 100 GiB of a
279 GiB RAID virtual disk (the outer third), and the profile spans ~170-195 MiB/s. A model's
bandwidth parameter therefore belongs to the table's own location, and every query cell can
look it up here (`table_bandwidth`) instead of measuring it again.

Method: read each target file with `dd iflag=direct bs=128k` (PostgreSQL's request size) in
512 MiB pieces; place each piece at the byte-weighted mean physical offset of its extents
(`filefrag -v`, FIEMAP). Reads only, files only (never a block device).

Cold below the OS, as for cold query runs: the RAID controller's write-back cache is evicted
first (cache.evict_device_cache), and each piece records its device read latency from
/proc/diskstats. dd keeps one 128 KiB request in flight, so a disk-served piece shows
~0.6-0.75 ms per request (128 KiB at 170-195 MiB/s) and a controller-cache hit ~0.1 ms.
Pieces under CACHE_HIT_MS are flagged and excluded from table_bandwidth.

    ./tpch bench location                         # fio file + lineitem of every loaded SF
    ./tpch bench location --table 1:orders --file /srv/tpch/bench/fio.dat --no-default

Output: results/bench/<stamp>_location/{location.csv, meta.json}
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .. import cache, sysinfo
from ..config import Config
from ..counters import parse_diskstats
from ..shell import Shell

CHUNK = 512 * 2**20
BS = 128 * 1024
FS_BLOCK = 4096
CACHE_HIT_MS = 0.3          # between ~0.1 ms (controller cache) and >= 0.6 ms (disk) per 128 KiB request
COLUMNS = ["target", "file", "offset_bytes", "bytes", "phys_GiB", "seconds", "MiBps",
           "dev_reads", "r_await_ms", "cache_hit"]


# --- placement -------------------------------------------------------------------------------

def parse_filefrag(text: str) -> list[tuple[int, int, int]]:
    """`filefrag -v -b4096` output -> [(logical_start, physical_start, length)] in bytes."""
    ex = []
    for line in text.splitlines():
        m = re.match(r"\s*\d+:\s+(\d+)\.\.\s*(\d+):\s+(\d+)\.\.\s*(\d+):", line)
        if m:
            l0, l1, p0, _ = map(int, m.groups())
            ex.append((l0 * FS_BLOCK, p0 * FS_BLOCK, (l1 - l0 + 1) * FS_BLOCK))
    return ex


def mean_physical(ex: list[tuple[int, int, int]], lo: int, hi: int) -> float:
    """Byte-weighted mean physical offset of the logical range [lo, hi)."""
    tot = acc = 0
    for l0, p0, n in ex:
        a, b = max(lo, l0), min(hi, l0 + n)
        if a < b:
            acc += (a - l0 + p0 + (b - l0 + p0)) / 2 * (b - a)
            tot += b - a
    return acc / tot if tot else float("nan")


def pieces(size: int, chunk: int = CHUNK) -> list[tuple[int, int]]:
    """(offset, length) pieces, BS-aligned for O_DIRECT; a trailing partial block is skipped."""
    return [(off, min(chunk, size - off) // BS * BS) for off in range(0, size - BS + 1, chunk)]


def dd_cmd(path: str, offset: int, length: int) -> list[str]:
    return ["dd", f"if={path}", "of=/dev/null", f"bs={BS}", f"skip={offset // BS}",
            f"count={length // BS}", "iflag=direct", "status=none"]


# --- targets ---------------------------------------------------------------------------------

def relation_files(cfg: Config, sf: int, table: str) -> list[str]:
    """All segment files (1 GiB each) of a table's main fork."""
    import psycopg
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname=cfg.dbname(sf)) as conn:
        rel = conn.execute("SELECT pg_relation_filepath(%s)", (table,)).fetchone()[0]
    base = Path(cfg.PGDATA) / rel
    segs = [base]
    while (nxt := Path(f"{base}.{len(segs)}")).exists():
        segs.append(nxt)
    return [str(p) for p in segs]


def default_targets(cfg: Config) -> list[tuple[str, list[str]]]:
    fio = Path(cfg.DATA_ROOT) / "bench" / "fio.dat"
    out = [("fio.dat", [str(fio)])] if fio.exists() else []
    return out + [(f"sf{sf}.lineitem", relation_files(cfg, sf, "lineitem")) for sf in cfg.sfs]


# --- measurement -----------------------------------------------------------------------------

def piece_row(target: str, path: str, offset: int, length: int, phys: float, seconds: float,
              disk: dict[str, int]) -> dict:
    r_await = disk["read_ms"] / disk["reads"] if disk["reads"] else None
    return {"target": target, "file": path, "offset_bytes": offset, "bytes": length,
            "phys_GiB": round(phys / 2**30, 3), "seconds": round(seconds, 4),
            "MiBps": round(length / 2**20 / seconds, 1), "dev_reads": disk["reads"],
            "r_await_ms": None if r_await is None else round(r_await, 3),
            "cache_hit": int(r_await is not None and r_await < CACHE_HIT_MS)}


def _disk(device: str) -> dict[str, int]:
    return parse_diskstats(Path("/proc/diskstats").read_text(), device)


def run_location_bench(cfg: Config, targets: list[tuple[str, list[str]]],
                       dry_run: bool = False) -> tuple[Path, Shell]:
    sh = Shell(dry_run)
    out = cfg.repo / cfg.RESULTS_DIR / "bench" / f"{datetime.now():%Y%m%d-%H%M%S}_location"
    cache.evict_device_cache(cfg, sh)
    if dry_run:
        for _, files in targets:
            for path in files:
                sh.run(["filefrag", "-v", "-b4096", path])
                sh.log.append(f"dd iflag=direct bs={BS} over {path} in {CHUNK >> 20} MiB pieces")
        return out, sh

    out.mkdir(parents=True)
    rows = []
    with open(out / "location.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for target, files in targets:
            for path in files:
                ex = parse_filefrag(subprocess.run(["filefrag", "-v", "-b4096", path], capture_output=True,
                                                   text=True, check=True).stdout)
                for off, n in pieces(Path(path).stat().st_size):
                    d0, t0 = _disk(cfg.IO_DEVICE), time.perf_counter()
                    sh.run(dd_cmd(path, off, n))
                    s, d1 = time.perf_counter() - t0, _disk(cfg.IO_DEVICE)
                    row = piece_row(target, path, off, n, mean_physical(ex, off, off + n), s,
                                    {k: d1[k] - d0[k] for k in ("reads", "read_ms")})
                    rows.append(row)
                    w.writerow(row)
                    f.flush()
                    print(f"{target:14s} {Path(path).name:10s} @{row['phys_GiB']:7.2f} GiB "
                          f"{row['MiBps']:6.1f} MiB/s  {row['r_await_ms']} ms/req"
                          + ("  CONTROLLER CACHE HIT" if row["cache_hit"] else ""), flush=True)
    (out / "meta.json").write_text(json.dumps({
        "targets": {t: files for t, files in targets}, "device": cfg.IO_DEVICE,
        "chunk_bytes": CHUNK, "bs": BS, "cache_hit_ms": CACHE_HIT_MS,
        "device_cache_evicted_GiB": cfg.int("DEVICE_CACHE_EVICT_GIB"),
        "device_cache_settle_s": cfg.int("DEVICE_CACHE_SETTLE_S"),
        "cache_hit_pieces": sum(r["cache_hit"] for r in rows), "sys": sysinfo.collect(),
    }, indent=2))
    return out, sh


# --- lookup for models -----------------------------------------------------------------------

def scans(cfg: Config) -> list[Path]:
    """Every location scan, oldest first."""
    return sorted((cfg.repo / cfg.RESULTS_DIR / "bench").glob("*_location/location.csv"))


def load_scan(path: Path) -> list[dict]:
    """Rows of one scan. Scans before eviction existed have no target/r_await columns."""
    rows = []
    for r in csv.DictReader(open(path)):
        rows.append({"target": r.get("target") or None, "file": r["file"], "bytes": int(r["bytes"]),
                     "phys_GiB": float(r["phys_GiB"]), "seconds": float(r["seconds"]),
                     "MiBps": float(r["MiBps"]),
                     "r_await_ms": float(r["r_await_ms"]) if r.get("r_await_ms") else None,
                     "cache_hit": r.get("cache_hit") == "1"})
    return rows


def evicted(path: Path) -> bool:
    meta = path.parent / "meta.json"
    return meta.exists() and json.loads(meta.read_text()).get("device_cache_evicted_GiB", 0) > 0


def table_bandwidth(rows: list[dict], match: str) -> float | None:
    """Sequential MiB/s over every disk-served piece whose file path contains `match`
    (total bytes / total time, i.e. what a full scan of those files gets)."""
    sel = [r for r in rows if match in r["file"] and not r["cache_hit"]]
    return sum(r["bytes"] for r in sel) / 2**20 / sum(r["seconds"] for r in sel) if sel else None
