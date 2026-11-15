"""Cache state control.

cold: stop PostgreSQL (empties shared_buffers) -> sync -> drop OS page cache -> start PostgreSQL
warm: the runner executes the query once, unmeasured, before the measured run

Below the OS there is a third cache: the RAID controller's write-back cache (PERC H710P, ~1 GiB).
Measured on kugenuma06 (queries/q6/sf1): it keeps recently WRITTEN data and serves it at
~1.1-1.4 GB/s; reads do not allocate in it; new writes evict old data. drop_caches cannot
empty it, so a freshly loaded or VACUUM FULL'd table reads "cold" at cache speed.
evict_device_cache() writes and deletes a scratch file larger than that cache. Because reads
never refill it, once per experiment (before the first cold run) is enough.
"""
from __future__ import annotations

import os
import time

from . import sysconf
from .config import Config
from .shell import Shell, root_prefix


def pg_ctl(cfg: Config, sh: Shell, *args: str, bind: bool | None = None) -> None:
    """bind: None = as SYSCONF says (pgnuma knob), True/False = force. A started postmaster
    inherits numactl's CPU and memory policy, and so do all its backends."""
    cmd = [f"{cfg.PGBIN}/pg_ctl", "-D", cfg.PGDATA, "-l", f"{cfg.DATA_ROOT}/postgres.log", "-w", *args]
    if os.geteuid() == 0:  # docker: the cluster belongs to the postgres OS user
        cmd = ["runuser", "-u", "postgres", "--", *cmd]
    node = sysconf.pg_numa_node(cfg) if bind is not False else None
    if bind and node is None:
        node = cfg.values.get("PG_NUMA_NODE") or None
    if node is not None:
        cmd = ["numactl", f"--cpunodebind={node}", f"--membind={node}", *cmd]
    sh.run(cmd)


def drop_os_cache(sh: Shell) -> None:
    sh.run(["sync"])
    sh.run([*root_prefix(), "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"])


def make_cold(cfg: Config, sh: Shell) -> None:
    pg_ctl(cfg, sh, "-m", "fast", "stop")
    drop_os_cache(sh)
    pg_ctl(cfg, sh, "start")


def evict_device_cache_cmds(cfg: Config) -> tuple[list[str], list[str]]:
    """(write, delete) commands for the scratch file under DATA_ROOT/bench (a file, never a device)."""
    path = f"{cfg.DATA_ROOT}/bench/evict.tmp"
    mib = cfg.int("DEVICE_CACHE_EVICT_GIB") * 1024
    write = ["dd", "if=/dev/zero", f"of={path}", "bs=1M", f"count={mib}", "oflag=direct", "conv=fsync",
             "status=none"]
    return write, ["rm", "-f", path]


def evict_device_cache(cfg: Config, sh: Shell) -> None:
    if cfg.int("DEVICE_CACHE_EVICT_GIB") <= 0:
        return
    write, delete = evict_device_cache_cmds(cfg)
    sh.run(["mkdir", "-p", f"{cfg.DATA_ROOT}/bench"])
    sh.run(write)
    sh.run(delete)
    # The controller destages the last written data in the background (invisible to the OS);
    # reads right after compete with it (measured: 106 instead of 185 MB/s). Wait it out.
    settle = cfg.int("DEVICE_CACHE_SETTLE_S")
    sh.log.append(f"sleep {settle}")
    if not sh.dry_run:
        time.sleep(settle)
