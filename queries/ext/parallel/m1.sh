#!/usr/bin/env bash
# E2 (parallel), M1 (docs/ext/parallel.md): query-independent measurements before any parallel query runs.
#   cores:1..8                  the clock with K busy cores on node 0 (30 s each)
#   par:{psync,uring}:0:P:450   a parallel scan of lineitem (PostgreSQL's chunking, cold, buffered) with Q6's work
#                               per read (~450 k cycles), P = 1, 2, 3, 4, 6, 8 participants: aggregate MiB/s vs P
#                               (the device under P interleaved streams), clock, busy CPUs
#   par:{psync,uring}:0:P:3000:90  Q1's work per read (~3.1 M cycles), P = 2, 4, 6, 8, 90 s each
# Over the job: turbostat (2 s), procmon. Read-only on lineitem's files; sysconf applied and restored by the driver.
#   src/setup/detach.sh parallel-m1 queries/ext/parallel/m1.sh      (~45 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m1
mkdir -p "$OUT"
TS="$OUT/turbostat.txt"
sudo -n turbostat --quiet --interval 2 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CPU%c1,CPU%c6,CoreTmp,PkgTmp,PkgWatt \
  --out "$TS" </dev/null >/dev/null 2>&1 &
$PY -m tpch.procmon --out "$OUT/procmon.jsonl" &
PM=$!
cleanup() {
  sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true
  kill "$PM" 2>/dev/null || true
}
trap cleanup EXIT
echo "== out $OUT"
sha256sum src/tpch/ext/parallel/*.py src/tpch/ext/parallel/*.c
$PY -m tpch.ext.parallel.bench --out "$OUT" --cells \
  cores:1 cores:2 cores:3 cores:4 cores:5 cores:6 cores:7 cores:8 \
  par:psync:0:1:450 par:psync:0:2:450 par:psync:0:3:450 par:psync:0:4:450 par:psync:0:6:450 par:psync:0:8:450 \
  par:uring:0:1:450 par:uring:0:2:450 par:uring:0:3:450 par:uring:0:4:450 par:uring:0:6:450 par:uring:0:8:450 \
  par:psync:0:2:3000:90 par:psync:0:4:3000:90 par:psync:0:6:3000:90 par:psync:0:8:3000:90 \
  par:uring:0:2:3000:90 par:uring:0:4:3000:90 par:uring:0:6:3000:90 par:uring:0:8:3000:90
echo "== done $OUT"
