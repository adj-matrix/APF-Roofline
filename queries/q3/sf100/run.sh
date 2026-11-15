#!/usr/bin/env bash
# Q3 × SF100 (sdb; temp files on sdb via temp_tablespaces), interleaved with SF10 cold (sda).
# Prediction (parts A and B written before measuring): docs/5-q3-sf100-prediction.md.
# "Warm" is not run: a table larger than memory is read from the device anyway (Q6 × SF100, A7).
# Long job: launch only detached,   src/setup/detach.sh q3-sf100 queries/q3/sf100/run.sh   (≈ 40 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
LABEL=${LABEL:-main}
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
./tpch experiment --query q3 --sf 10 100 --cache cold --mode plain   --blocks 5 --label "$LABEL"
./tpch experiment --query q3 --sf 10 100 --cache cold --mode analyze --blocks 1 --label "$LABEL"
