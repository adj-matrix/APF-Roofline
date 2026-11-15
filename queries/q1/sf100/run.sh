#!/usr/bin/env bash
# Q1 × SF100 (lineitem on sdb), interleaved with SF10 cold (sda) as the same-session calibration.
# Prediction (part A written before measuring): docs/6-q1-sf100-prediction.md.
# "Warm" is not run: a table larger than memory is read from the device anyway (Q6 × SF100, A7).
# Long job: launch only detached,   src/setup/detach.sh q1-sf100 queries/q1/sf100/run.sh   (≈ 90 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
LABEL=${LABEL:-main}
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
./tpch experiment --query q1 --sf 10 100 --cache cold --mode plain   --blocks 5 --label "$LABEL"
./tpch experiment --query q1 --sf 10 100 --cache cold --mode analyze --blocks 1 --label "$LABEL"
