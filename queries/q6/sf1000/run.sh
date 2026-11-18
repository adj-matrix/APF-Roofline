#!/usr/bin/env bash
# Q6 x SF1000, blind test of the frozen model (src/tpch/model, params model/params.json).
# The prediction (model/predictions/q6_sf1000.json) was written before this runs; its hash and content are
# printed into the job log first, so the log proves what was predicted before any measurement.
# SF100 cold runs interleaved as the same-session control (model/predictions/q6_sf100_control.json).
# Long job: src/setup/detach.sh q6-sf1000 queries/q6/sf1000/run.sh   (~85 min: 3 x (~24 + ~2.5 min) + cold-cache preparation)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
echo "== registered predictions (written before the runs)"
sha256sum model/params.json model/predictions/q6_sf1000.json model/predictions/q6_sf100_control.json
python3 -c "
import json
for f in ('model/predictions/q6_sf1000.json', 'model/predictions/q6_sf100_control.json'):
    p = json.load(open(f))
    print(f, 'T', round(p['T'], 1), 'range', [round(x) for x in p['T_range']], 'C', round(p['C'] / 1e9, 1), 'G',
          'f', round(p['f'], 3), 'W', round(p['W'], 1), 'params', p['params_digest'])"
echo "== measurement"
./tpch experiment --query q6 --sf 100 1000 --cache cold --mode plain --blocks 3 --label blind
