#!/usr/bin/env bash
# E3 (attribution), A1 (docs/ext/attribution.md): where the user cycles go, by component, as parallel workers are
# added. Q6 and Q1 x SF100 cold, sync / off, n = 0, 1, 3, 7 workers, one run each, in random order. Each run under a
# system-wide perf record of cycles:u and instructions:u (period 20,000,003), kept in RAM (/dev/shm) and reduced to
# a per-component JSON (src/tpch/ext/attribution/profile.py, postgres processes only) in the bench directory.
# Settings RESET on exit.     src/setup/detach.sh attribution-a1 queries/ext/attribution/a1.sh      (~40 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_attribution_a1
mkdir -p "$OUT"
cleanup() {
  sudo -n pkill -INT -f -- "perf record -q -a -e cycles:u,instructions:u" 2>/dev/null || true
  sudo -n rm -f /dev/shm/attribution_a1_*.data
  $PY -m tpch.pgserver reset || true
}
trap cleanup EXIT
echo "== out $OUT"
sha256sum src/tpch/ext/attribution/*.py results/tools/postgres-18.6-11dd4ccc.syms
$PY -m tpch.pgserver set sync off
for qn in $(printf '%s\n' q6:0 q6:1 q6:3 q6:7 q1:0 q1:1 q1:3 q1:7 | shuf); do
  q=${qn%%:*} n=${qn#*:} data=/dev/shm/attribution_a1_${q}_n$n.data
  echo "== $q n=$n"
  sudo -n perf record -q -a -e cycles:u,instructions:u -c 20000003 -o "$data" &
  sleep 1
  ./tpch experiment --query "$q" --sf 100 --cache cold --mode plain --blocks 1 \
    --set max_parallel_workers_per_gather=$n --label "attribution_n$n"
  sudo -n pkill -INT -f -- "perf record -q -a -e cycles:u,instructions:u" || true
  sleep 3
  $PY -m tpch.ext.attribution.profile "$data" --out "$OUT/${q}_n$n.json"
  sudo -n rm -f "$data"
done
echo "== done $OUT"
