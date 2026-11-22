"""E2 (parallel), M1: query-independent measurements for parallel scans (docs/ext/parallel.md, M1).

Cells:
  cores:K                        K processes of pure work on node 0 (replica_par none): the clock with K busy cores
  leapq:DIRECT:PHASE:READERS:DUR[:OWN_FD[:BATCH]]  leapq.c: two leapfrogging participants (64 MiB chunks, PHASE MiB out
                                 of phase, 16 in flight each, no work) whose reads go through one shared FIFO queue
                                 to READERS reader threads (io_method = worker); cold first when buffered
  par:ENGINE:DIRECT:P:WORK[:DUR] replica_par on lineitem's segments of tpch_sf100 (read-only): P participants of a
                                 parallel sequential scan (PostgreSQL's chunking), ENGINE psync | uring, WORK
                                 thousand iterations of fixed work per 128 KiB read; cold like a PostgreSQL run
                                 (sync; drop_caches = 3 first); stops at the end of the table or after DUR s
Per cell: perf stat over all participants (inherited: cycles, user cycles, ref-cycles, task-clock -> clock,
busy CPUs), the replica's JSON (reads, MiB/s, per-participant blocks and end times), t0 / t1 for the job's
turbostat and procmon logs. Writes <out>/cells.jsonl.
  PYTHONPATH=src .venv/bin/python -m tpch.ext.parallel.bench --out results/bench/<ts>_parallel_m1 --cells ...
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg

EVENTS = "task-clock,cycles,cycles:u,ref-cycles,instructions,context-switches,cpu-migrations"
BIND = ["numactl", "--cpunodebind=0", "--membind=0"]


def perf_totals(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        p = line.split(",")
        if len(p) > 3 and p[0] and p[0][0].isdigit():
            out[p[2]] = float(p[0])
    return out


def lineitem_list(cfg, dest: Path) -> None:
    """lineitem's segment files of tpch_sf100, in order (read-only)."""
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname="tpch_sf100", autocommit=True) as conn:
        rel = conn.execute("SELECT pg_relation_filepath('lineitem')").fetchone()[0]
        datadir = conn.execute("SHOW data_directory").fetchone()[0]
    base = Path(datadir) / rel
    files = [base] + sorted((p for p in base.parent.glob(base.name + ".*") if p.suffix[1:].isdigit()),
                            key=lambda p: int(p.suffix[1:]))
    dest.write_text("".join(f"{f}\n" for f in files))


def cell(spec: str, out: Path, tag: str, binary: Path) -> dict:
    parts = spec.split(":")
    if parts[0] == "cores":
        args, dur = ["x", parts[1], "none", "0", "1", "400", "30"], 30
    elif parts[0] == "leapq":
        _, direct, phase, readers, dur = parts[:5]
        own = parts[5] if len(parts) > 5 else "1"
        batch = parts[6] if len(parts) > 6 else "1"
        args = ["@" + str(out / "lineitem.list"), direct, phase, readers, dur, "64", "4", own, batch]
        binary = out / "leapq"
        if direct == "0":
            subprocess.run(["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"], check=True)
            time.sleep(2)
    else:
        _, engine, direct, p, work = parts[:5]
        dur = parts[5] if len(parts) > 5 else "200"
        args = ["@" + str(out / "lineitem.list"), p, engine, direct, "16", work, dur]
        subprocess.run(["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"], check=True)
        time.sleep(2)
    perf_out = out / f"perf_{tag}.csv"
    t0 = time.time()
    r = subprocess.run(["sudo", "-n", "env", "LC_ALL=C", "perf", "stat", "-x,", "-o", str(perf_out), "-e", EVENTS, "--",
                        *BIND, str(binary), *args],
                       capture_output=True, text=True, check=True)
    t1 = time.time()
    rj = json.loads(r.stdout.strip().splitlines()[-1])
    c = perf_totals(perf_out)
    if parts[0] == "leapq":
        rj.setdefault("part_end_s", [rj["elapsed_s"]])
    return {**rj, "cycles": c["cycles"], "user_cycles": c["cycles:u"], "ref_cycles": c["ref-cycles"],
            "task_clock_s": c["task-clock"] / 1000, "cs": c.get("context-switches", 0), "t0": t0, "t1": t1,
            "GHz": c["cycles"] / c["ref-cycles"] * 2.7, "busy_cpus": c["task-clock"] / 1000 / rj["elapsed_s"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cells", nargs="+", required=True)
    ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args()
    from ... import config, sysconf
    from ...shell import Shell
    cfg = config.load()
    a.out.mkdir(parents=True, exist_ok=True)
    if any(c.startswith(("par:", "leapq:")) for c in a.cells):
        lineitem_list(cfg, a.out / "lineitem.list")
    binary = a.out / "replica_par"
    subprocess.run(["cc", "-O2", "-Wall", "-o", str(binary), str(Path(__file__).with_name("replica_par.c"))], check=True)
    subprocess.run(["cc", "-O2", "-Wall", "-pthread", "-o", str(a.out / "leapq"), str(Path(__file__).with_name("leapq.c"))],
                   check=True)
    seed = random.randrange(2**31)
    rng = random.Random(seed)
    order = []
    for r in range(a.reps):
        blk = list(a.cells)
        rng.shuffle(blk)
        order += [(c, r) for c in blk]
    (a.out / f"design_{int(time.time())}.json").write_text(json.dumps({"seed": seed, "order": order}))
    sh = Shell(False)
    with sysconf.applied(cfg, sh), open(a.out / "cells.jsonl", "a") as f:
        for spec, r in order:
            tag = f"{spec.replace(':', '_')}_r{r}"
            res = {"cell": spec, "rep": r, **cell(spec, a.out, tag, binary)}
            f.write(json.dumps(res) + "\n")
            f.flush()
            print(f"{tag:28s} GHz {res['GHz']:.3f} busy {res['busy_cpus']:.2f} MiB/s {res['MiBps']:.0f} "
                  f"elapsed {res['elapsed_s']:.1f} ends {res['part_end_s']}", flush=True)
            time.sleep(2)
    print("system settings (applied, then restored):\n  " + "\n  ".join(sh.log))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    main()
