#!/usr/bin/env bash
# E2 (parallel), M5 (docs/ext/parallel.md): scan.lineitem_light (TPC-H-free) under worker / off n = 1, 2, 3 and
# io_uring / data n = 3: the device rate of these cells at Q6's work per read, to predict Q6 (rule registered in the
# docs before this job). One run each. Settings RESET on exit.
#   src/setup/detach.sh parallel-m5 queries/ext/parallel/m5.sh      (~12 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m5
mkdir -p "$OUT"
$PY -m tpch.procmon --out "$OUT/procmon.jsonl" &
PM=$!
cleanup() { kill "$PM" 2>/dev/null || true; $PY -m tpch.pgserver reset || true; }
trap cleanup EXIT
echo "== out $OUT"
$PY -m tpch.pgserver set worker off
for n in $(printf '%s\n' 1 2 3 | shuf); do
  echo "== scan.lineitem_light worker/off n=$n"
  ./tpch experiment --query scan.lineitem_light --sf 100 --cache cold --mode plain --blocks 1 \
    --set max_parallel_workers_per_gather=$n --label "parallel_m5_worker_off_n$n"
done
$PY -m tpch.pgserver set io_uring data
echo "== scan.lineitem_light io_uring/data n=3"
./tpch experiment --query scan.lineitem_light --sf 100 --cache cold --mode plain --blocks 1 \
  --set max_parallel_workers_per_gather=3 --label "parallel_m5_io_uring_data_n3"
echo "== done $OUT"
