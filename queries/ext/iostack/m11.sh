#!/usr/bin/env bash
# E1 fix, M11 (docs/ext/iostack.md, M10 results): same-session control for v3's worker / off miss. v3 (from the
# TPC-H-free scans of 2026-11-22) predicts Q6 worker / off 100.7 s at 2.94 GHz; Q6's own runs (2026-11-19) took
# 95.0 s at 3.03-3.11 GHz. Is that a day-to-day drift (today's scans also ran 2 % slower in worker / data and
# sync / data than Q6 did on 11-19) or a real difference between Q6 and a scan with the same cycles per read?
# Q6 worker / off and scan.lineitem_light worker / off, 2 each, alternating in random order. Not used to fit.
# Settings RESET on exit.     src/setup/detach.sh iostack-m11 queries/ext/iostack/m11.sh      (~12 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_iostack_m11
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
$PY -m tpch.ext.iostack.server set worker off
for b in 1 2; do
  for q in $(printf '%s\n' q6 scan.lineitem_light | shuf); do
    echo "== $q worker/off (block $b)"
    ./tpch experiment --query "$q" --sf 100 --cache cold --mode plain --blocks 1 --label iostack_m11_worker_off
  done
done
echo "== done $OUT"
