#!/usr/bin/env bash
# Blind tests of the read-back revision (params 4619dfadeec0: phase-3 wait = extents x wait per extent, from
# hardware/readback_cpu.sh and hardware/layout_rate.sh; gamma removed). Predictions were written before the runs.
#   1. Q3 x SF100, work_mem 32MB (128 batches; training has 512 / 256 / 64 / 16): ~25 min, label blind32.
#   2. Q3 x SF1000, work_mem 256MB, enable_nestloop=off (128 batches, ~12.7 MiB per file per writeback: above
#      mballoc's 8 MiB window, the other branch of the extent model), with SF100 at the same settings
#      interleaved as the same-session control: ~4.5 h, label blind256. turbostat as in run_128mb.sh.
# The model code is not committed: the sha256 of src/tpch/model/*.py is printed and recorded in .vscode/plan.md.
#   src/setup/detach.sh q3-night queries/q3/sf1000/run_night.sh      (~5 h)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
PRED=(model/predictions/q3_sf100_32MB.json model/predictions/q3_sf1000_256MB.json
      model/predictions/q3_sf100_control_256MB.json)
echo "== registered predictions (written before the runs)"
sha256sum model/params.json "${PRED[@]}" src/tpch/model/*.py
python3 -c "
import json, sys
for f in sys.argv[1:]:
    p = json.load(open(f)); q = p['parts']
    print(f, 'T', round(p['time_s'], 1), 'range', [round(x) for x in p['time_range_s']], 'phase12', round(q['phase12_s']),
          'phase3', round(q['phase3_s']), 'W_jump', round(q['W_jump_s'], 1), 'extents', round(q['phase3_extents']),
          'params', p['params_digest'])" "${PRED[@]}"

echo "== 1. SF100 work_mem 32MB"
./tpch experiment --query q3 --sf 100 --cache cold --mode plain --blocks 3 --label blind32 --set work_mem=32MB

echo "== 2. SF1000 work_mem 256MB (SF100 control interleaved)"
TS="results/jobs/$(date +%Y%m%d-%H%M%S)_q3-night_turbostat.txt"
sudo -n turbostat --quiet --interval 10 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,CPU%c1,CPU%c6 \
  --out "$TS" </dev/null >/dev/null 2>&1 &
# kill turbostat itself: sudo ignores signals from its own process group (see run.sh)
trap 'sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true' EXIT
echo "== turbostat -> $TS"
./tpch experiment --query q3 --sf 100 1000 --cache cold --mode plain --blocks 3 --label blind256 \
  --set work_mem=256MB --set enable_nestloop=off
