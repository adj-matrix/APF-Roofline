#!/usr/bin/env bash
# Q1 × SF1 and SF10 in the same experiments: CPU-heavy scan; is its cost the sum of its pieces?
# Prediction (written before measuring): docs/3-q1-prediction.md. Pieces: queries/q1/micro.sql (label micro).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
LABEL=${LABEL:-main}

gov=$(sort -u /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor)
[[ "$gov" == schedutil ]] || { echo "governor is '$gov', expected schedutil (plan §4.11)" >&2; exit 1; }

# piece microbenchmarks (only if absent): SF1 warm, every statement of micro.sql as one factor
if ! ls results/experiments/*_q1_micro.json >/dev/null 2>&1; then
    ./tpch experiment --query $(grep '^-- name:' queries/q1/micro.sql | sed 's/-- name: /q1./') \
        --sf 1 --cache warm --mode plain --blocks 5 --label micro
fi
./tpch experiment --query q1 --sf 1 10 --cache cold warm --mode plain   --blocks 5 --label "$LABEL"
./tpch experiment --query q1 --sf 1 10 --cache cold warm --mode analyze --blocks 1 --label "$LABEL"
