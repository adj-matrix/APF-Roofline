#!/usr/bin/env bash
# E1 fix, M8 (docs/ext/iostack.md, "Fix", M8): why PostgreSQL's worker / off keeps ~3 cores out of C6 and runs at
# ~3.05 GHz where the pool replica keeps ~2.15 and runs at ~3.37. Query-independent: no TPC-H query here.
#   PostgreSQL: scan.lineitem_light (count(l_comment) over all of lineitem, SF100) under worker / off (2 runs),
#               worker / data and sync / off (1 each): the I/O workers' CPU per read, kswapd's, at the device rate.
#   Replica:    poolfull (cold like PostgreSQL: machine-wide drop_caches, one full pass of lineitem, counted over
#               the whole run) at THINK 40 and 140 us, buffered (2 each) and direct (1 each).
#   Over the job: turbostat (2 s) and procmon (CPU per process category: io_worker, kswapd, replica, ...).
# Settings RESET on exit.        src/setup/detach.sh iostack-m8 queries/ext/iostack/m8.sh        (~30 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
TS0=$(date +%Y%m%d-%H%M%S)
OUT=results/bench/${TS0}_iostack_m8
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
task() {
  case $1 in
    pg:*)
      IFS=: read -r _ m d <<<"$1"
      echo "== pg scan.lineitem_light $m/$d"
      $PY -m tpch.ext.iostack.server set "$m" "$d"
      ./tpch experiment --query scan.lineitem_light --sf 100 --cache cold --mode plain --blocks 1 --label "iostack_m8_${m}_${d}"
      $PY -m tpch.ext.iostack.server reset
      ;;
    rep:*)
      echo "== replica ${1#rep:}"
      $PY -m tpch.ext.iostack.replica --out "$OUT" --cells "${1#rep:}" --reps 1
      ;;
  esac
}
for b in 1 2; do
  for t in $(printf '%s\n' pg:worker:off rep:poolfull:0:3:40:18 rep:poolfull:0:3:140:18 | shuf); do task "$t"; done
done
for t in $(printf '%s\n' pg:worker:data pg:sync:off rep:poolfull:1:3:40:18 rep:poolfull:1:3:140:18 | shuf); do task "$t"; done
echo "== done $OUT"
