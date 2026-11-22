#!/usr/bin/env bash
# E1 fix, held-out test of model v2 (docs/ext/iostack.md, "Fix"): SF100 conditions the fix never looked at,
# predicted before the runs (model/ext/iostack/v2/heldout_q{6,1}.json, hashes printed below):
#   Q6 worker / data and worker / off with io_workers = 1 (one worker: does it keep up?), Q1 worker / data,
#   Q1 io_uring / off. 2 blocks each, random order; turbostat over the job. Settings RESET on exit.
#   src/setup/detach.sh iostack-heldout queries/ext/iostack/heldout.sh       (~55 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
echo "== registered predictions (written before the runs)"
sha256sum model/params.json model/ext/iostack/v2/heldout_q6.json model/ext/iostack/v2/heldout_q1.json src/tpch/ext/iostack/*.py src/tpch/ext/iostack/*.c
TS="results/jobs/$(date +%Y%m%d-%H%M%S)_iostack-heldout_turbostat.txt"
sudo -n turbostat --quiet --interval 10 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,CPU%c1,CPU%c6 \
  --out "$TS" </dev/null >/dev/null 2>&1 &
cleanup() {
  sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true
  $PY -m tpch.ext.iostack.server reset || true
}
trap cleanup EXIT
cell() {   # query method direct label [extra]
  echo "== $1 $2/$3 ${5:-}"
  $PY -m tpch.ext.iostack.server set "$2" "$3" ${5:-}
  ./tpch experiment --query "$1" --sf 100 --cache cold --mode plain --blocks 1 --label "$4"
}
for b in 1 2; do
  for c in $(printf '%s\n' q6:worker:data:w1 q6:worker:off:w1 q1:worker:data:w3 q1:io_uring:off:w3 | shuf); do
    IFS=: read -r q m d w <<<"$c"
    if [[ $w == w1 ]]; then cell "$q" "$m" "$d" "iostack_${m}_${d}_w1" io_workers=1
    else cell "$q" "$m" "$d" "iostack_${m}_${d}"; fi
  done
done
