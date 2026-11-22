#!/usr/bin/env bash
# E1 fix, M9 (docs/ext/iostack.md, "Fix", M9): the replica's work per read as fixed cycles instead of fixed time.
# A query's work per read is fixed in cycles, so a faster clock shortens it and lowers the on-CPU share that
# schedutil sees; the busy wait of M7 is fixed in time and has no such feedback. Where the clock curve is steep
# (sync / data, D ~ 0.7) that changes the clock. On lineitem's segments (O_DIRECT, read-only):
#   psync direct, fixed work 300k..600k iterations (~1 cycle each), the M7 time-based THINK 200 as a same-session
#   control, and io_uring direct at the Q6 point. 2 reps, 60 s each, random order.
# Plus PostgreSQL's own light scan (scan.lineitem_light, no TPC-H query) under sync / data (the steep point) and
# worker / off (with procmon now per io worker: how PostgreSQL spreads the reads over its workers).
# Over the job: turbostat (2 s), procmon. Settings RESET on exit.
#   src/setup/detach.sh iostack-m9 queries/ext/iostack/m9.sh      (~35 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_iostack_m9
mkdir -p "$OUT"
TS="$OUT/turbostat.txt"
sudo -n turbostat --quiet --interval 2 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CPU%c1,CPU%c6,CoreTmp,PkgTmp,PkgWatt \
  --out "$TS" </dev/null >/dev/null 2>&1 &
$PY -m tpch.ext.iostack.procmon --out "$OUT/procmon.jsonl" &
PM=$!
cleanup() {
  sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true
  kill "$PM" 2>/dev/null || true
  $PY -m tpch.ext.iostack.server reset || true
}
trap cleanup EXIT
echo "== out $OUT"
sha256sum src/tpch/ext/iostack/*.py src/tpch/ext/iostack/*.c queries/scan/micro.sql
pg() {   # method direct
  echo "== pg scan.lineitem_light $1/$2"
  $PY -m tpch.ext.iostack.server set "$1" "$2"
  ./tpch experiment --query scan.lineitem_light --sf 100 --cache cold --mode plain --blocks 1 --label "iostack_m9_$1_$2"
  $PY -m tpch.ext.iostack.server reset
}
pg sync data
$PY -m tpch.ext.iostack.replica --out "$OUT" --reps 2 --dur 60 --cells \
  li-io:psync:1:1:300k li-io:psync:1:1:380k li-io:psync:1:1:420k li-io:psync:1:1:460k li-io:psync:1:1:500k \
  li-io:psync:1:1:600k li-io:psync:1:1:200 li-io:uring:1:16:420k li-io:uring:1:16:460k
pg worker off
echo "== done $OUT"
