#!/usr/bin/env bash
# E2 (parallel), M6 (docs/ext/parallel.md): why worker mode switches between a fast and a slow device regime under
# a parallel scan. scan.lineitem_light (TPC-H-free) with n = 1 (2 participants): worker / off x 3 runs and
# sync / off x 1 (control: steady interleaved rate). Each run under a block-layer trace of sdb: every request's
# issue time (perf clock, monotonic), issuing task, sector, size (perf record -e block:block_rq_issue), kept in RAM
# (/dev/shm, nothing written to sdb while tracing) and converted to a CSV in the bench directory afterwards.
# Settings RESET on exit.     src/setup/detach.sh parallel-m6 queries/ext/parallel/m6.sh      (~15 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m6
mkdir -p "$OUT"
DEV=$(( (8 << 20) | 16 ))          # sdb = 8:16 in the kernel's dev_t encoding
cleanup() {
  sudo -n pkill -INT -f -- "perf record -q -e block:block_rq_issue" 2>/dev/null || true
  sudo -n rm -f /dev/shm/parallel_m6_*.data
  $PY -m tpch.pgserver reset || true
}
trap cleanup EXIT
echo "== out $OUT"
run() {   # method tag
  local tag=$2 data=/dev/shm/parallel_m6_$2.data
  sudo -n perf record -q -e block:block_rq_issue --filter "dev == $DEV" -a -o "$data" &
  sleep 1
  ./tpch experiment --query scan.lineitem_light --sf 100 --cache cold --mode plain --blocks 1 \
    --set max_parallel_workers_per_gather=1 --label "parallel_m6_$tag"
  sudo -n pkill -INT -f -- "perf record -q -e block:block_rq_issue" || true
  sleep 3
  sudo -n perf script -i "$data" -F tid,time,trace 2>/dev/null \
    | awk '{gsub(":","",$2); print $2","$1","$(NF-3)","$(NF-1)}' > "$OUT/trace_$tag.csv"
  sudo -n rm -f "$data"
  echo "   trace: $(wc -l < "$OUT/trace_$tag.csv") requests"
}
$PY -m tpch.pgserver set worker off
for i in 1 2 3; do echo "== light worker/off n=1 #$i"; run worker "worker_off_$i"; done
$PY -m tpch.pgserver set sync off
echo "== light sync/off n=1"; run sync "sync_off_1"
echo "== done $OUT"
