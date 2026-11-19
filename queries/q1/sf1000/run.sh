#!/usr/bin/env bash
# Q1 x SF1000, blind test of the current model (params model/params.json, 4619dfadeec0). One block (user's
# choice, 2026-11-19: three blocks take too long now), with SF100 cold interleaved as the same-session control.
# The predictions (model/predictions/q1_sf1000.json, q1_sf100_control.json) were written before this runs; their
# hashes and content go into the job log first. The model code is not committed: sha256 of src/tpch/model/*.py.
# turbostat as in queries/q3/sf1000/run_night.sh (clock of the long run).
#   src/setup/detach.sh q1-sf1000 queries/q1/sf1000/run.sh      (~2.3 h)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
PRED=(model/predictions/q1_sf1000.json model/predictions/q1_sf100_control.json)
echo "== registered predictions (written before the runs)"
sha256sum model/params.json "${PRED[@]}" src/tpch/model/*.py
python3 -c "
import json, sys
for f in sys.argv[1:]:
    p = json.load(open(f)); q = p['parts']
    print(f, 'T', round(p['time_s'], 1), 'range', [round(x) for x in p['time_range_s']], 'cycles', round(p['cycles'] / 1e9), 'G',
          'clock', round(p['clock_ghz'], 3), 'wait', round(p['wait_s'], 1), 'params', p['params_digest'])" "${PRED[@]}"
TS="results/jobs/$(date +%Y%m%d-%H%M%S)_q1-sf1000_turbostat.txt"
sudo -n turbostat --quiet --interval 10 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,CPU%c1,CPU%c6 \
  --out "$TS" </dev/null >/dev/null 2>&1 &
# kill turbostat itself: sudo ignores signals from its own process group (see run.sh)
trap 'sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true' EXIT
echo "== turbostat -> $TS"
./tpch experiment --query q1 --sf 100 1000 --cache cold --mode plain --blocks 1 --label blind
