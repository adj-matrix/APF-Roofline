#!/usr/bin/env bash
# CPU frequency setting as a factor (plan §4.21): which governor / turbo setting is stage 2's baseline,
# and the two frequency puzzles from stage 1:
#   P1  `performance` doubled the backend's CPU time on cold Q6 × SF10 (wall unchanged), warm 11 % slower
#   P6  ~1 s warm queries under `schedutil` often never reached turbo
# Prediction (written before running): docs/hardware.md §7.
#
# Levels (governor:no_turbo): schedutil:0 = stage 1 baseline (system default), performance:0,
# schedutil:1, performance:1 (fixed 2.7 GHz). Each run gets its setting right before it; the original
# setting is restored at the end (sysconf knob cpufreq). Randomized blocks, as every experiment.
#   src/setup/detach.sh cpufreq hardware/cpufreq.sh          (≈ 50 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
LABEL=${LABEL:-cpufreq}
LEVELS=(schedutil:0 performance:0 schedutil:1 performance:1)
BLOCKS=${BLOCKS:-3}

# Q6: SF1 warm (~1 s, P6), SF10 cold (disk-bound, P1), SF1 cold, SF10 warm
./tpch experiment --query q6 --sf 1 10 --cache cold warm --cpufreq "${LEVELS[@]}" --blocks "$BLOCKS" --label "$LABEL"
# Q1: CPU-bound even cold
./tpch experiment --query q1 --sf 1 10 --cache cold warm --cpufreq "${LEVELS[@]}" --blocks "$BLOCKS" --label "$LABEL"
