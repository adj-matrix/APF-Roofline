"""Disk read microbenchmark with fio.

Safety: the benchmark file is created once (the only write), and every measurement
runs with fio --readonly. Nothing ever targets a block device.

Output: results/bench/<stamp>_io/
  meta.json            sweep definition, device queue settings, environment
  raw/<test>.json      fio JSON output
  raw/<test>/          iostat/mpstat during the test (request sizes that reached the device)
  io.csv               one row per test (schema: IO_COLUMNS)
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .. import sysinfo
from ..cache import drop_os_cache
from ..config import Config
from ..monitor import Monitors
from ..parse.sysstat import parse_iostat
from ..shell import Shell

GiB = 1 << 30
IO_COLUMNS = ["test", "pattern", "direct", "bs_bytes", "threads", "runtime_s", "io_bytes",
              "MiBps", "iops", "lat_avg_ms", "lat_p99_ms", "dev_rareq_kB", "dev_util_pct"]


@dataclass(frozen=True)
class IoTest:
    pattern: str          # "seq" | "rand"
    bs: int               # bytes
    direct: bool          # O_DIRECT (bypass page cache)
    threads: int = 1

    @property
    def name(self) -> str:
        kb = f"{self.bs // 1024}k" if self.bs < (1 << 20) else f"{self.bs >> 20}m"
        return f"{self.pattern}_{'direct' if self.direct else 'buffered'}_{kb}_t{self.threads}"


def default_sweep() -> list[IoTest]:
    K = 1024
    seq_direct = [IoTest("seq", b * K, True) for b in (4, 8, 16, 32, 64, 128, 256, 512, 1024)]
    seq_buffered = [IoTest("seq", b * K, False) for b in (8, 128)]   # 8k = PG block; 128k = io_combine_limit
    rand_direct = [IoTest("rand", b * K, True) for b in (4, 8, 16, 64)]
    return seq_direct + seq_buffered + rand_direct


def fio_cmd(t: IoTest, path: Path, offset: int, region: int, runtime_s: int) -> list[str]:
    return [
        "fio", f"--name={t.name}", f"--filename={path}", "--readonly",
        f"--rw={'read' if t.pattern == 'seq' else 'randread'}", f"--bs={t.bs}",
        f"--direct={int(t.direct)}", "--ioengine=psync", f"--numjobs={t.threads}",
        f"--offset={offset}", f"--size={region}", f"--runtime={runtime_s}", "--time_based",
        "--group_reporting", "--randrepeat=0", "--output-format=json",
    ]


def prepare_file_cmd(path: Path, size: int) -> list[str]:
    """Lay out the benchmark file with incompressible data (the other write: cache.evict_device_cache)."""
    return ["fio", "--name=prepare", f"--filename={path}", "--rw=write", "--bs=1m", "--direct=1",
            "--ioengine=psync", f"--size={size}", "--refill_buffers", "--output-format=terse"]


def parse_fio(doc: dict) -> dict:
    """fio --output-format=json (group_reporting) -> one result row (read side)."""
    job = doc["jobs"][0]
    rd = job["read"]
    runtime_s = rd["runtime"] / 1000
    pct = rd["clat_ns"].get("percentile", {})
    return {
        "runtime_s": runtime_s,
        "io_bytes": rd["io_bytes"],
        "MiBps": rd["bw_bytes"] / (1 << 20),
        "iops": rd["iops"],
        "lat_avg_ms": rd["lat_ns"]["mean"] / 1e6,
        "lat_p99_ms": pct.get("99.000000", 0) / 1e6,
    }


def device_queue(dev: str) -> dict:
    q = Path("/sys/block") / dev / "queue"
    keys = ("rotational", "read_ahead_kb", "max_sectors_kb", "nr_requests", "scheduler")
    return {k: (q / k).read_text().strip() for k in keys if (q / k).exists()}


def run_io_bench(cfg: Config, file_size: int = 16 * GiB, region: int = 4 * GiB,
                 runtime_s: int = 15, tests: list[IoTest] | None = None,
                 dry_run: bool = False, volume: str = "data") -> tuple[Path, Shell]:
    """volume: `data` (DATA_ROOT on IO_DEVICE) or `big` (BIG_ROOT on BIG_IO_DEVICE)."""
    tests = tests or default_sweep()
    sh = Shell(dry_run)
    root, device = cfg.volume(volume)
    path = Path(root) / "bench" / "fio.dat"
    out = cfg.repo / cfg.RESULTS_DIR / "bench" / (f"{datetime.now():%Y%m%d-%H%M%S}_io"
                                                  + ("" if volume == "data" else f"_{volume}"))

    if dry_run or not (path.exists() and path.stat().st_size == file_size):
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
        sh.run(prepare_file_cmd(path, file_size))

    rows = []
    for i, t in enumerate(tests):
        # Rotate over aligned regions (O_DIRECT needs aligned offsets). Consecutive tests never
        # share a region, so the RAID controller cache (~1 GiB) holds nothing useful.
        offset = (i % (file_size // region)) * region
        cmd = fio_cmd(t, path, offset, region, runtime_s)
        drop_os_cache(sh)
        if dry_run:
            sh.run(cmd)
            continue
        raw = out / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        with Monitors(raw / t.name, device=device, interval=1):
            time.sleep(1)
            doc = json.loads(sh.run(cmd))
        (raw / f"{t.name}.json").write_text(json.dumps(doc, indent=1))
        io = [r for r in parse_iostat((raw / t.name / "iostat.log").read_text(), device)
              if r["r/s"] > 0]
        reads = sum(r["r/s"] for r in io)
        row = {"test": t.name, **asdict(t), "bs_bytes": t.bs, **parse_fio(doc),
               "dev_rareq_kB": sum(r["rkB/s"] for r in io) / reads if reads else None,
               "dev_util_pct": sum(r["%util"] for r in io) / len(io) if io else None}
        rows.append(row)
        print(f"{t.name:28s} {row['MiBps']:9.1f} MiB/s {row['iops']:9.0f} IOPS "
              f"lat {row['lat_avg_ms']:7.3f} ms  dev req {row['dev_rareq_kB'] or 0:6.1f} kB", flush=True)

    if not dry_run:
        with open(out / "io.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=IO_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        (out / "meta.json").write_text(json.dumps({
            "file": str(path), "file_size": file_size, "region": region, "runtime_s": runtime_s,
            "tests": [t.name for t in tests], "device": device, "volume": volume,
            "queue": {d.name: device_queue(d.name) for d in sorted(Path("/sys/block").iterdir())
                      if not d.name.startswith(("loop", "sr"))},
            "sys": sysinfo.collect(),
        }, indent=2))
    return out, sh
