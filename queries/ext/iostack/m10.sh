#!/usr/bin/env bash
# E1 fix, M10 (docs/ext/iostack.md, "Fix", M10): PostgreSQL's own scans as the query-independent replica for the
# two cells where the C replica misses PostgreSQL's dynamics (worker / off: how the workers share the reads;
# sync / data: the clock where its curve is steepest). No TPC-H query: three scans of all of lineitem with
# different work per tuple (queries/scan/micro.sql: lineitem_min < lineitem_light < lineitem_mid), each under
# worker / off and sync / data, 2 blocks, random order. Model v3 reads clock and waits from these at the query's
# own user cycles per read (code written and hashed before this job's data is looked at).
# Over the job: turbostat (2 s), procmon (per io worker). Settings RESET on exit.
#   src/setup/detach.sh iostack-m10 queries/ext/iostack/m10.sh      (~40 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_iostack_m10
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
sha256sum src/tpch/ext/iostack/*.py queries/scan/micro.sql
for b in 1 2; do
  for c in $(printf '%s\n' {lineitem_min,lineitem_light,lineitem_mid}:{worker:off,sync:data} | shuf); do
    IFS=: read -r q m d <<<"$c"
    echo "== pg scan.$q $m/$d (block $b)"
    $PY -m tpch.ext.iostack.server set "$m" "$d"
    ./tpch experiment --query "scan.$q" --sf 100 --cache cold --mode plain --blocks 1 --label "iostack_m10_${m}_${d}"
    $PY -m tpch.ext.iostack.server reset
  done
done
echo "== done $OUT"
