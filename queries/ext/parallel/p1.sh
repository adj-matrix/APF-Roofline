#!/usr/bin/env bash
# E2 (parallel), P1 (docs/ext/parallel.md): Q6 x SF100 cold with n = 1, 2, 3, 5, 7 parallel workers (n + 1
# participants), io_method sync and io_uring, buffered; one run each, n shuffled within a method, methods in random
# order. Predictions registered before the run: model/ext/parallel/q6_p1.json (from the M1 replica).
# Over the job: turbostat (2 s), procmon (all postgres processes: leader + workers). Settings RESET on exit.
#   src/setup/detach.sh parallel-p1 queries/ext/parallel/p1.sh      (~25 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_p1
mkdir -p "$OUT"
TS="$OUT/turbostat.txt"
sudo -n turbostat --quiet --interval 2 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CPU%c1,CPU%c6,CoreTmp,PkgTmp,PkgWatt \
  --out "$TS" </dev/null >/dev/null 2>&1 &
$PY -m tpch.procmon --out "$OUT/procmon.jsonl" &
PM=$!
cleanup() {
  sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true
  kill "$PM" 2>/dev/null || true
  $PY -m tpch.pgserver reset || true
}
trap cleanup EXIT
echo "== out $OUT"
echo "== registered predictions (written before the runs)"
sha256sum model/ext/parallel/q6_p1.json
for m in $(printf '%s\n' sync io_uring | shuf); do
  $PY -m tpch.pgserver set "$m" off
  for n in $(printf '%s\n' 1 2 3 5 7 | shuf); do
    echo "== q6 $m/off n=$n"
    ./tpch experiment --query q6 --sf 100 --cache cold --mode plain --blocks 1 \
      --set max_parallel_workers_per_gather=$n --label "parallel_${m}_n$n"
  done
done
echo "== done $OUT"
