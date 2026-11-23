#!/usr/bin/env bash
# E3 (attribution), A2 (docs/ext/attribution.md): is the per-page cost a constant across queries and tables?
# Cold, SF100, sync / off, n = 0, one run each in random order: Q6 and Q1 (the reference, now with the corrected
# component rules and the full symbol report kept), and three TPC-H-free scans held out: scan.lineitem_min,
# scan.orders_light (57.4 tuples per page), scan.customer_light (41.7; lineitem 53.3). perf record of cycles:u and
# instructions:u, period 2,000,003 (10x A1, for the small tables). Settings RESET on exit.
#   src/setup/detach.sh attribution-a2 queries/ext/attribution/a2.sh      (~25 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_attribution_a2
mkdir -p "$OUT"
cleanup() {
  sudo -n pkill -INT -f -- "perf record -q -a -e cycles:u,instructions:u" 2>/dev/null || true
  sudo -n rm -f /dev/shm/attribution_a2_*.data
  $PY -m tpch.pgserver reset || true
}
trap cleanup EXIT
echo "== out $OUT"
sha256sum src/tpch/ext/attribution/*.py results/tools/postgres-18.6-11dd4ccc.syms queries/scan/micro.sql
$PY -m tpch.pgserver set sync off
for q in $(printf '%s\n' q6 q1 scan.lineitem_min scan.orders_light scan.customer_light | shuf); do
  data=/dev/shm/attribution_a2_$q.data
  echo "== $q"
  sudo -n perf record -q -a -e cycles:u,instructions:u -c 2000003 -o "$data" &
  sleep 1
  ./tpch experiment --query "$q" --sf 100 --cache cold --mode plain --blocks 1 --label attribution_a2
  sudo -n pkill -INT -f -- "perf record -q -a -e cycles:u,instructions:u" || true
  sleep 3
  $PY -m tpch.ext.attribution.profile "$data" --out "$OUT/$q.json" --raw "$OUT/$q.report.txt"
  sudo -n rm -f "$data"
done
echo "== done $OUT"
