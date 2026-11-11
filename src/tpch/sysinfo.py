"""Environment snapshot stored in every run's meta.json."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

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
        "mem_total_kb": _meminfo_kb("MemTotal"),
        "git_commit": _cmd("git", "rev-parse", "HEAD", cwd=REPO),
        "git_dirty": bool(_cmd("git", "status", "--porcelain", cwd=REPO)),
    }
