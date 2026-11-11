"""Parsers for sysstat 12.x text output produced by the commands in tpch.monitor.

Every row gets `ts` (float epoch seconds, end of the sampling interval) plus the
tool's own column names as keys, numeric values converted to float.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_HEADER_DATE = re.compile(r"\t(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\s")


def _num(s: str) -> float | str:
    try:
        return float(s)
    except ValueError:
        return s


def _header_date(line: str) -> datetime:
    m = _HEADER_DATE.search(line)
    if not m:
        raise ValueError(f"no date in sysstat header: {line!r}")
    d = m.group(1)
    return datetime.strptime(d, "%Y-%m-%d" if "-" in d else "%m/%d/%Y")


def parse_iostat(text: str, device: str | None = None) -> list[dict]:
    """`iostat -x -t -d -y` with S_TIME_FORMAT=ISO. One row per (timestamp, device)."""
    rows: list[dict] = []
    ts: float | None = None
    cols: list[str] | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("Linux "):
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}T", s):
            ts = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp()
        elif s.startswith("Device"):
            cols = s.split()
        elif cols and ts is not None:
            parts = s.split()
            if len(parts) != len(cols) or (device and parts[0] != device):
                continue
            row = {"ts": ts, "device": parts[0]}
            row.update({c: _num(v) for c, v in zip(cols[1:], parts[1:])})
            rows.append(row)
    return rows


def parse_mpstat(text: str) -> list[dict]:
    """`mpstat -P ALL N`. Times are HH:MM:SS only: date comes from the header, midnight rollover handled."""
    rows: list[dict] = []
    day: datetime | None = None
    prev_secs = -1
    cols: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("Linux "):
            day = _header_date(line)
            continue
        parts = line.split()
        if not parts or parts[0].startswith("Average"):
            continue
        if "CPU" in parts and "%idle" in parts:
            cols = parts[parts.index("CPU"):]
            continue
        if cols is None or day is None or len(parts) != len(cols) + 1:
            continue
        hh, mm, ss = (int(x) for x in parts[0].split(":"))
        secs = hh * 3600 + mm * 60 + ss
        if secs < prev_secs:
            day += timedelta(days=1)
        prev_secs = secs
        ts = (day + timedelta(seconds=secs)).astimezone().timestamp()
        row = {"ts": ts, "CPU": parts[1]}
        row.update({c: _num(v) for c, v in zip(cols[1:], parts[2:])})
        rows.append(row)
    return rows


def parse_pidstat(text: str) -> list[dict]:
    """`pidstat -u -d -h -H -p PID N`. Header lines start with '#'; first column is epoch seconds."""
    rows: list[dict] = []
    cols: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            cols = line[1:].split()
            continue
        parts = line.split()
        if cols is None or len(parts) != len(cols) or not parts[0].isdigit():
            continue
        row = {"ts": float(parts[0])}
        row.update({c: _num(v) for c, v in zip(cols[1:], parts[1:])})
        rows.append(row)
    return rows
