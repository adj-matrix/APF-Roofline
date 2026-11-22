#!/usr/bin/env bash
# E1 fix, measurements (docs/ext/iostack.md, "Fix"). Nothing here is fitted to the SF100 E1 runs.
#  1. M7 replicas (src/tpch/ext/iostack/replica.py, ~35 min): the I/O paths of one backend reproduced on our own
#     file with a fixed busy wait per 128 KiB read -> per read: clock, kernel cycles in the issuing thread,
#     wait; io-wq / reader CPU; the backend's clock while K I/O workers read at a given rate.
#  2. SF10 cold (small SF, ~55 min): the backend's user cycles when it issues its reads itself vs when the I/O
#     workers do (dU per read), and the per-read kernel cycles of PostgreSQL's own O_DIRECT / io_uring paths.
#     Q6: 6 cells x 3 blocks; Q1: 4 cells x 3 blocks; then one Q6 block with extra user-mode events
#     (TLB, branch, L1d misses) to attribute dU.
# PostgreSQL settings are RESET on exit.        src/setup/detach.sh iostack-fix queries/ext/iostack/fix.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
cleanup() { $PY -m tpch.ext.iostack.server reset || true; }
trap cleanup EXIT
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_iostack_m7
echo "== M7 replicas -> $OUT"
$PY -m tpch.ext.iostack.replica --out "$OUT" --reps 2 --dur 25
cell() {   # query method direct label
  echo "== $1 $2/$3 ($4)"
  $PY -m tpch.ext.iostack.server set "$2" "$3"
  ./tpch experiment --query "$1" --sf 10 --cache cold --mode plain --blocks 1 --label "$4_$2_$3"
}
for b in 1 2 3; do
  for c in $(printf '%s\n' sync:off sync:data worker:off worker:data io_uring:off io_uring:data | shuf); do
    cell q6 "${c%%:*}" "${c#*:}" iostack
  done
  for c in $(printf '%s\n' sync:off worker:off sync:data io_uring:data | shuf); do
    cell q1 "${c%%:*}" "${c#*:}" iostack
  done
done
export PERF_EVENTS="task-clock,cycles,instructions,ref-cycles,cycles:u,instructions:u,dTLB-load-misses:u,iTLB-load-misses:u,branch-misses:u,L1-dcache-load-misses:u,L1-icache-load-misses:u,LLC-load-misses:u,context-switches,cpu-migrations,page-faults,major-faults"
for c in $(printf '%s\n' sync:off worker:off io_uring:data worker:data | shuf); do
  cell q6 "${c%%:*}" "${c#*:}" iostack_attr
done
echo "replicas: $OUT"
