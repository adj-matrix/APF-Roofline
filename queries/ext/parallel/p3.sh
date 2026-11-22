#!/usr/bin/env bash
# E2 (parallel), P3 (docs/ext/parallel.md): the third io_method and O_DIRECT near the ridge. worker / off
# (3 I/O workers shared by all participants), sync / data, io_uring / data; Q6 with n = 1, 2, 3 and Q1 with n = 5, 7;
# one run each, shuffled within a method, methods in random order. Predictions registered before the run:
# model/ext/parallel/p3.json. Over the job: turbostat, procmon (per io worker). Settings RESET on exit.
#   src/setup/detach.sh parallel-p3 queries/ext/parallel/p3.sh      (~40 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_p3
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
sha256sum model/ext/parallel/p3.json
for md in $(printf '%s\n' worker:off sync:data io_uring:data | shuf); do
  m=${md%%:*} d=${md#*:}
  $PY -m tpch.pgserver set "$m" "$d"
  for qn in $(printf '%s\n' q6:1 q6:2 q6:3 q1:5 q1:7 | shuf); do
    q=${qn%%:*} n=${qn#*:}
    echo "== $q $m/$d n=$n"
    ./tpch experiment --query "$q" --sf 100 --cache cold --mode plain --blocks 1 \
      --set max_parallel_workers_per_gather=$n --label "parallel_${m}_${d}_n$n"
  done
done
echo "== done $OUT"
