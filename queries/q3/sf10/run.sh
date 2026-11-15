#!/usr/bin/env bash
# Q3 × SF1 and SF10 in the same experiments: does Q3 scale linearly like Q6?
# Prediction (written before measuring): docs/2-q3-prediction.md. Calibration: label `cal` (SF1 warm only).
# Device and memory parameters: hardware/ (./tpch bench location, ./tpch bench mem).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
LABEL=${LABEL:-main}

gov=$(sort -u /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor)
[[ "$gov" == schedutil ]] || { echo "governor is '$gov', expected schedutil (plan §4.11)" >&2; exit 1; }

# calibration (only if absent): SF1 warm, before the prediction's part B
if ! ls results/experiments/*_q3_cal.json >/dev/null 2>&1; then
    ./tpch experiment --query q3 --sf 1 --cache warm --mode plain   --blocks 3 --label cal
    ./tpch experiment --query q3 --sf 1 --cache warm --mode analyze --blocks 1 --label cal
fi
# 5 blocks × {SF1, SF10} × {cold, warm}, shuffled within each block; evicts the controller cache first
./tpch experiment --query q3 --sf 1 10 --cache cold warm --mode plain   --blocks 5 --label "$LABEL"
./tpch experiment --query q3 --sf 1 10 --cache cold warm --mode analyze --blocks 1 --label "$LABEL"
