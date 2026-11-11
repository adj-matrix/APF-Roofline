"""Exact before/after kernel counters for a query (totals); sysstat samples are only for shape.

  /proc/diskstats      device reads, sectors, busy time (io_ticks)
  /proc/<pid>/stat     backend utime / stime (clock ticks)
  /proc/<pid>/io       backend bytes actually read from storage
"""
from __future__ import annotations

import os
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK")
SECTOR = 512
# /proc/diskstats columns after (major, minor, name); see Documentation/admin-guide/iostats.rst
_DISK_FIELDS = ["reads", "reads_merged", "sectors_read", "read_ms",
                "writes", "writes_merged", "sectors_written", "write_ms",
                "in_flight", "io_ticks_ms", "weighted_ms"]


def parse_diskstats(text: str, device: str) -> dict[str, int]:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 14 and parts[2] == device:
            return dict(zip(_DISK_FIELDS, map(int, parts[3:14])))
    raise KeyError(f"device {device} not in /proc/diskstats")


def parse_pid_stat(text: str) -> dict[str, float]:
    # comm (field 2) may contain spaces: split after the closing parenthesis.
    rest = text[text.rindex(")") + 2:].split()
    utime, stime = int(rest[11]), int(rest[12])   # fields 14, 15 overall
    return {"utime_s": utime / CLK_TCK, "stime_s": stime / CLK_TCK}


def parse_pid_io(text: str) -> dict[str, int]:
    kv = dict(line.split(": ") for line in text.splitlines() if ": " in line)
    return {"read_bytes": int(kv["read_bytes"]), "rchar": int(kv["rchar"])}


def snapshot(device: str, pid: int) -> dict:
    proc = Path("/proc")
    snap = {"disk": parse_diskstats((proc / "diskstats").read_text(), device)}
    try:
        snap["proc"] = parse_pid_stat((proc / str(pid) / "stat").read_text())
        snap["proc"].update(parse_pid_io((proc / str(pid) / "io").read_text()))
    except (PermissionError, FileNotFoundError):
        pass  # backend owned by another user (docker: postgres): device counters only
    return snap


def delta(before: dict, after: dict) -> dict:
    return {
        group: {k: after[group][k] - before[group][k] for k in before[group]}
        for group in before if group in after
    }
