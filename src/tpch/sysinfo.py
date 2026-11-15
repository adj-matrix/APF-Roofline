"""Environment snapshot stored in every run's meta.json."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from . import sysconf
from .config import REPO


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cmd(*cmd: str, cwd: Path | None = None) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _cpu_model() -> str | None:
    for line in (_read("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def _meminfo_kb(key: str) -> int | None:
    for line in (_read("/proc/meminfo") or "").splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1])
    return None


def _disk_cache_types() -> dict[str, str | None]:
    """'write back' / 'write through' per block device, as the RAID controller reports it."""
    out = {}
    for d in sorted(Path("/sys/class/scsi_disk").glob("*")):
        blocks = list((d / "device" / "block").glob("*"))
        if blocks:
            out[blocks[0].name] = _read(str(d / "cache_type"))
    return out


def _postmaster_affinity() -> dict | None:
    """CPUs and memory nodes the running postmaster (and so every backend) may use."""
    try:
        from .config import load
        pid = Path(load().PGDATA, "postmaster.pid").read_text().split()[0]
    except (OSError, IndexError, AttributeError):
        return None
    out = {"pid": int(pid)}
    for line in (_read(f"/proc/{pid}/status") or "").splitlines():
        k, _, v = line.partition(":")
        if k == "Cpus_allowed_list":
            out["cpus"] = v.strip()
    # numactl --membind sets a memory policy (not a cpuset), visible per mapping in numa_maps
    policies = {line.split()[1] for line in (_read(f"/proc/{pid}/numa_maps") or "").splitlines() if line.strip()}
    out["mempolicy"] = ",".join(sorted(policies)) or None
    return out


def collect() -> dict:
    return {
        "host": platform.node(),
        "kernel": platform.release(),
        "cpu_model": _cpu_model(),
        "cpus_online": _read("/sys/devices/system/cpu/online"),
        "numa_nodes": _read("/sys/devices/system/node/online"),
        "governor": _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "no_turbo": _read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "thp": _read("/sys/kernel/mm/transparent_hugepage/enabled"),
        **sysconf.state(),                  # nmi_watchdog, timers_active (see sysconf.py)
        "postmaster": _postmaster_affinity(),
        "disk_cache_type": _disk_cache_types(),
        "mem_total_kb": _meminfo_kb("MemTotal"),
        "git_commit": _cmd("git", "rev-parse", "HEAD", cwd=REPO),
        "git_dirty": bool(_cmd("git", "status", "--porcelain", cwd=REPO)),
    }
