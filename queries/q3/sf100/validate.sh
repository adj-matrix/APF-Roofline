#!/usr/bin/env bash
# Q3 × SF100, test of model v2 (docs/5-q3-sf100-prediction.md, Part D, written before this runs):
# work_mem 256MB -> 16 batches (32 temp files instead of 128), nearly the same spill volume (15/16 vs 63/64).
# 64MB, the configuration v2 was built on, runs interleaved as the control. Cold, 3 blocks, shuffled per block.
#   src/setup/detach.sh q3-sf100-v2 queries/q3/sf100/validate.sh      (≈ 45 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
for b in 1 2 3; do
  for wm in $(printf '%s\n' 64MB 256MB | shuf); do
    ./tpch experiment --query q3 --sf 100 --cache cold --mode plain --blocks 1 --set work_mem="$wm" --label "v2_wm$wm"
  done
done
./tpch experiment --query q3 --sf 100 --cache cold --mode analyze --blocks 1 --set work_mem=256MB --label v2_wm256MB
