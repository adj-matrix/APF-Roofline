#!/usr/bin/env bash
# Clock and scan-wait microbenchmark for the unified model (docs/7-unified-model.md, Part A).
# Read-only on $BIG_ROOT/bench/fio.dat; the write cell appends to a file in $BIG_ROOT/bench/spill and deletes it.
# SYSCONF is applied for the duration and restored (hardware/clock_state.py uses sysconf.applied).
#   src/setup/detach.sh clock-state hardware/clock_state.sh        (≈ 30 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_clock_state
.venv/bin/python hardware/clock_state.py --out "$OUT" ${LOADS:+--loads $LOADS} --reps ${REPS:-3} --dur 60 --read-file "$BIG_ROOT/bench/fio.dat"
echo "results: $OUT"
