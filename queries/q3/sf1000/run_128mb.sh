#!/usr/bin/env bash
# Q3 x SF1000, second blind test (after the C1 revision, params 04da4632d83a): work_mem 128MB,
# enable_nestloop=off (as in run.sh: otherwise Nested Loop + Memoize at SF1000). 256 / 8 batches instead of
# 512 / 16, and batch files twice as large as in the first blind test: checks the revised terms where they
# differ from 64MB -- the batch-count write term (N = 256), the read-back overlap gamma by batch-file size
# (608 MiB per batch, outside the trained range: clamped to SF100 / 64MB's 216 MiB; indexed by N instead it
# would be 0.86, predicting 4,690 s instead of 4,462 s) and the false-hash-match term (SF^2).
# SF100 with the same settings interleaved as the same-session control.
# The model code is not committed yet: the sha256 of src/tpch/model/*.py at registration is printed too and
# recorded in .vscode/plan.md.
# turbostat (read-only, every 10 s): per-core clock, temperature, package power.
# Long job: src/setup/detach.sh q3-sf1000-128mb queries/q3/sf1000/run_128mb.sh   (~4 h)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
PRED=(model/predictions/q3_sf1000_128MB.json model/predictions/q3_sf100_control_128MB.json)
echo "== registered predictions (written before the runs)"
sha256sum model/params.json "${PRED[@]}" src/tpch/model/*.py
python3 -c "
import json, sys
for f in sys.argv[1:]:
    p = json.load(open(f)); q = p['parts']
    print(f, 'T', round(p['time_s'], 1), 'range', [round(x) for x in p['time_range_s']],
          'C', round(p['cycles'] / 1e9, 1), 'G', 'f', round(p['clock_ghz'], 3), 'W', round(p['wait_s'], 1),
          'phase12', round(q['phase12_s']), 'phase3', round(q['phase3_s']), 'params', p['params_digest'])" "${PRED[@]}"
TS="results/jobs/$(date +%Y%m%d-%H%M%S)_q3-sf1000-128mb_turbostat.txt"
sudo -n turbostat --quiet --interval 10 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,CPU%c1,CPU%c6 \
  --out "$TS" </dev/null >/dev/null 2>&1 &
# kill turbostat itself: sudo ignores signals from its own process group (see run.sh)
trap 'sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true' EXIT
echo "== turbostat -> $TS"
echo "== measurement"
./tpch experiment --query q3 --sf 100 1000 --cache cold --mode plain --blocks 3 --label blind128 \
  --set work_mem=128MB --set enable_nestloop=off
