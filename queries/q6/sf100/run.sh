#!/usr/bin/env bash
# Q6 × SF100 (lineitem on sdb, 86 GiB > 62 GiB RAM), interleaved with SF10 (sda) as the calibration.
# Prediction (written before measuring): docs/4-q6-sf100-prediction.md. Device parameters: docs/hardware.md §6.
# Long job: launch only detached,   src/setup/detach.sh q6-sf100 queries/q6/sf100/run.sh   (≈ 55 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
LABEL=${LABEL:-main}

# SYSCONF (config.env) applies watchdog/timers/pgnuma/cpufreq for each command and restores them after.
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
# evicts the controller cache on sda and sdb first; SF100 runs sample sdb, SF10 runs sda
./tpch experiment --query q6 --sf 10 100 --cache cold warm --mode plain   --blocks 5 --label "$LABEL"
./tpch experiment --query q6 --sf 10 100 --cache cold warm --mode analyze --blocks 1 --label "$LABEL"
