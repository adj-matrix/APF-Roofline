#!/usr/bin/env bash
# Q3 x SF1000, blind test of the frozen model. Settings: work_mem 64MB (the cluster default) and
# enable_nestloop=off: at SF1000 the planner would otherwise pick Nested Loop + Memoize + Index Scan on orders,
# a plan shape the model is not calibrated for. With it off, the plan has SF100's shape (512 batches).
# SF100 runs with the same settings interleaved as the same-session control.
# turbostat (read-only, every 10 s) runs alongside: per-core clock, temperature, package power, to look at the
# clock drop seen after ~10 min in Q6 x SF1000.
# Long job: src/setup/detach.sh q3-sf1000 queries/q3/sf1000/run.sh   (~4 h)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
echo "== registered predictions (written before the runs)"
sha256sum model/params.json model/predictions/q3_sf1000.json model/predictions/q3_sf100_control.json
python3 -c "
import json
for f in ('model/predictions/q3_sf1000.json', 'model/predictions/q3_sf100_control.json'):
    p = json.load(open(f))
    print(f, 'T', round(p['T'], 1), 'range', [round(x) for x in p['T_range']], 'C', round(p['C'] / 1e9, 1), 'G',
          'f', round(p['f'], 3), 'W', round(p['W'], 1), 'params', p['params_digest'])"
TS="results/jobs/$(date +%Y%m%d-%H%M%S)_q3-sf1000_turbostat.txt"
sudo -n turbostat --quiet --interval 10 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,CPU%c1,CPU%c6 \
  --out "$TS" </dev/null >/dev/null 2>&1 &
# Kill turbostat itself, not the sudo in front of it: sudo ignores signals sent from its own process
# group, so `kill <sudo pid>` from this script did nothing, and turbostat (holding the log pipe through its
# inherited stdout) kept the job alive for 5 h after the measurement (2026-11-18).
trap 'sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true' EXIT
echo "== turbostat -> $TS"
echo "== measurement"
./tpch experiment --query q3 --sf 100 1000 --cache cold --mode plain --blocks 3 --label blind \
  --set work_mem=64MB --set enable_nestloop=off
