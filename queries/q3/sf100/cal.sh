#!/usr/bin/env bash
# Q3 × SF100, part B calibration: a scale model at SF10 (warm, sda), before any SF100 run.
# The variant q3.sf100_shape forces SF100's plan shape, (lineitem ⋈ orders) ⋈ customer. The orders hash
# table is then ~335 MiB, so work_mem sets how much of the join spills to temp files:
#   64MB -> 4 batches (3/4 of both sides spilled), 128MB -> 2 batches (1/2), 1GB -> 1 batch (none).
# The slope of cycles against spilled tuples is the CPU cost of a spilled tuple.
# Prediction and its use: docs/5-q3-sf100-prediction.md.
#   src/setup/detach.sh q3-sf100-cal queries/q3/sf100/cal.sh      (≈ 15 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
WMS=(64MB 128MB 1GB)
# work_mem is a connection setting, not a design factor: interleave by hand, a shuffled order per block
for b in 1 2 3; do
  for wm in $(printf '%s\n' "${WMS[@]}" | shuf); do
    ./tpch experiment --query q3.sf100_shape --sf 10 --cache warm --mode plain --blocks 1 \
        --set join_collapse_limit=1 --set work_mem="$wm" --label "cal_wm$wm"
  done
done
# SF10's own plan under the stage-2 settings, for comparison with stage 1 (949 cycles per lineitem tuple)
./tpch experiment --query q3 --sf 10 --cache warm --mode plain --blocks 3 --label cal_own
for wm in "${WMS[@]}"; do
  ./tpch experiment --query q3.sf100_shape --sf 10 --cache warm --mode analyze --blocks 1 \
      --set join_collapse_limit=1 --set work_mem="$wm" --label "cal_wm$wm"
done
