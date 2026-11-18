"""Compare sampling profiles (perf record, PERF_RECORD_HZ) of two runs, per function and per phase.

Diagnosis only: the cost model does not read profiles. A run's samples are split at the phase boundary of
its timeline (data.Run.phases), so a function that grows only in the read-back phase shows there.

    python -m tpch.model.profile RUN_SMALL RUN_BIG [--top 25] [--symfs DIR --kallsyms FILE]

prints, per phase, the cycles per function in each run, the big run's cycles divided by the size ratio
(SF_big / SF_small) and the difference: the functions that do more than linear work at the big SF.

Samples are taken on cpu-clock (time on CPU), and the clock differs between runs and phases (the read-back
phase of SF1000 runs at ~2.5 GHz, SF100's at ~3.3), so each sample is weighted by the clock of the second
it falls in (per-second cycles / task-clock, PERF_INTERVAL_MS): the table compares cycles, the model's unit.

The PGDG postgres binary is stripped: without its debug symbols most of the backend's samples are
"[unknown user]". To resolve them, extract the matching postgresql-NN-dbgsym package (same version as the
installed server; check the build-id) into a directory, symlink the postgres binary and libc into it at their
real paths, copy /proc/kallsyms (needs root), and pass --symfs DIR --kallsyms FILE; perf then runs as the
user. Without them perf runs through sudo (kernel symbols need root: kptr_restrict).
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import subprocess
from pathlib import Path

from .data import Run
from ..shell import root_prefix


def load_run(path: Path) -> Run:
    return Run(path, json.loads((path / "meta.json").read_text()), json.loads((path / "summary.json").read_text()))


def clock_by_second(run: Run) -> dict[int, float]:
    """{absolute second: GHz} from the per-second cycles and task-clock in timeline.csv.

    A timeline row is keyed on the absolute second its perf interval ends in (summarize.build_timeline).
    """
    t0 = run.meta["t_start"]
    out = {}
    with open(run.dir / "timeline.csv") as f:
        for row in csv.DictReader(f):
            task_ms, cycles = float(row.get("perf_task_ms") or 0), float(row.get("perf_cycles") or 0)
            if task_ms > 50:     # a few ms on CPU give a noisy ratio; such seconds have few samples anyway
                out[int(round(float(row["t_s"]) + t0))] = cycles / (task_ms * 1e6)
    return out


def samples_by_phase(run: Run, symfs: Path | None = None,
                     kallsyms: Path | None = None) -> dict[str, collections.Counter]:
    """{"scan": Counter(symbol -> Gcycles), "readback": ...}.

    Each sample counts 1/hz seconds on CPU times the clock of its second (the run's median clock where a
    second has no per-second cycles).

    Args:
        run: a run recorded with PERF_RECORD_HZ > 0.
        symfs, kallsyms: debug-symbol directory and a copy of /proc/kallsyms (see the module docstring);
            without them perf script runs through sudo.
    """
    hz = run.meta["perf_record_hz"]
    t0, t1 = run.meta["t_start"], run.meta["t_end"]
    boundary = t0 + (run.phases["p3_start_s"] or run.wall)
    clocks = clock_by_second(run)
    fallback = sorted(clocks.values())[len(clocks) // 2] if clocks else run.ghz
    perf_t0 = run.meta.get("perf_t0") or t0
    cmd = ["perf", "script", "-f", "-i", str(run.dir / "perf.data"), "-F", "time,ip,sym", "--no-demangle"]
    if symfs:
        cmd += [f"--symfs={symfs}"] + ([f"--kallsyms={kallsyms}"] if kallsyms else [])
    else:
        cmd = root_prefix() + cmd
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    counts = {"scan": collections.Counter(), "readback": collections.Counter()}
    for line in out.splitlines():
        head, _, rest = line.strip().partition(":")
        try:
            t = float(head)
        except ValueError:
            continue
        if not t0 <= t <= t1:
            continue
        ip, _, sym = rest.strip().partition(" ")
        sym = sym.strip().split("+")[0].removesuffix(".lto_priv.0")
        if not sym or sym == "[unknown]":
            sym = "[unknown kernel]" if ip.startswith("ffff") else "[unknown user]"
        second = int(perf_t0 + math.ceil(t - perf_t0))      # the second its perf interval ends in
        ghz = clocks.get(second, fallback)
        counts["scan" if t < boundary else "readback"][sym] += ghz / hz
    return counts


def compare(small: Run, big: Run, top: int = 25, symfs: Path | None = None, kallsyms: Path | None = None) -> str:
    """Markdown tables, one per phase: Gcycles per function, big / (SF ratio), and the excess."""
    ratio = big.meta["sf"] / small.meta["sf"]
    cs, cb = samples_by_phase(small, symfs, kallsyms), samples_by_phase(big, symfs, kallsyms)
    sf_s, sf_b = small.meta["sf"], big.meta["sf"]
    out = []
    for phase in ("scan", "readback"):
        s, b = cs[phase], cb[phase]
        ts, tb = sum(s.values()), sum(b.values())
        out.append(f"\n## {phase}: SF{sf_s} {ts:.0f} Gcycles, SF{sf_b} {tb:.0f} Gcycles "
                   f"(x{ratio:.0f} linear: {ts * ratio:.0f}, excess {tb - ts * ratio:+.0f} = "
                   f"{(tb / (ts * ratio) - 1) * 100 if ts else 0:+.1f}%)\n")
        out.append(f"| function | SF{sf_s} Gc | SF{sf_b} Gc | / {ratio:.0f} | growth | excess Gc | share of excess |")
        out.append("|---|---|---|---|---|---|---|")
        excess = {k: b.get(k, 0) - s.get(k, 0) * ratio for k in set(s) | set(b)}
        total = sum(excess.values())
        for k in sorted(excess, key=lambda k: -abs(excess[k]))[:top]:
            growth = f"{b.get(k, 0) / (s[k] * ratio) - 1:+.0%}" if s.get(k) else "new"
            out.append(f"| {k} | {s.get(k, 0):.1f} | {b.get(k, 0):.1f} | {b.get(k, 0) / ratio:.2f} | {growth} | "
                       f"{excess[k]:+.1f} | {excess[k] / total if total else 0:+.0%} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("small", type=Path)
    ap.add_argument("big", type=Path)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--symfs", type=Path, help="directory with the binaries' debug symbols at their real paths")
    ap.add_argument("--kallsyms", type=Path, help="a copy of /proc/kallsyms (with --symfs)")
    a = ap.parse_args()
    print(compare(load_run(a.small), load_run(a.big), a.top, a.symfs, a.kallsyms))


if __name__ == "__main__":
    main()
