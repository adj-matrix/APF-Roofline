#!/usr/bin/env bash
# Step 3 of SF100: re-check of the unified model (docs/7-unified-model.md, Part F, written before this runs).
# Q3 × SF100 at work_mem 128MB (32 batches: a condition no model part was fitted to) and Q6 × SF100, cold,
# 3 blocks, the order of the two shuffled per block; then one EXPLAIN ANALYZE of Q3 at 128MB.
#   src/setup/detach.sh sf100-recheck queries/sf100/recheck.sh      (≈ 35 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
for b in 1 2 3; do
  for q in $(printf '%s\n' q3 q6 | shuf); do
    if [[ $q == q3 ]]; then
      ./tpch experiment --query q3 --sf 100 --cache cold --mode plain --blocks 1 --set work_mem=128MB --label u_wm128MB
    else
      ./tpch experiment --query q6 --sf 100 --cache cold --mode plain --blocks 1 --label u_recheck
    fi
  done
done
./tpch experiment --query q3 --sf 100 --cache cold --mode analyze --blocks 1 --set work_mem=128MB --label u_wm128MB
