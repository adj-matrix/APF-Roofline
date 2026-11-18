#!/usr/bin/env bash
# Q3 x SF1000 revision, step C1: where do SF1000's extra user-mode cycles go? (diagnosis, not a test)
# The blind runs used 8.8 % more user-mode cycles than the revised model predicts (BIGINT keys included;
# the number of batches ruled out at SF100). One SF100 and one SF1000 run of the blind condition (64MB,
# enable_nestloop off), each with a sampling profile of the backend (perf record, cpu-clock 199 Hz,
# wall-clock timestamps): compare per function, scan phase and read-back phase separately.
#   src/setup/detach.sh q3-profile queries/q3/sf1000/profile.sh      (~1.7 h)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
PERF_RECORD_HZ=199 ./tpch experiment --query q3 --sf 100 1000 --cache cold --mode plain --blocks 1 --label profile \
  --set work_mem=64MB --set enable_nestloop=off
