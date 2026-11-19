#!/usr/bin/env bash
# M1 (docs/8-model-framework.md §7): the clock as a function of the on-CPU share D, the length L of each idle
# and the kind of idle (timer sleep vs synchronous device read), to replace the two fitted clock curves.
# Grid: sleep:D:L for D = 0.6 / 0.75 / 0.9 / 0.97 x L = 0.1 / 0.3 / 1 / 3 / 13 ms; dread:D (random 128 KiB
# O_DIRECT reads of our own fio.dat, ~13 ms each) for D = 0.6 / 0.75 / 0.9, to compare with sleep at L = 13 ms;
# spin and spin+wakers:1 / 3 as anchors to the earlier runs (docs/7). 2 blocks, random order, 40 s per cell.
# Predictions (written before the run, 2026-11-19):
#   P1 at a fixed D, the clock rises with L (few long idles cost less than many short ones; smoke test:
#      L 0.1 ms at D 0.72 -> 1.68 GHz, L 3 ms at D 0.80 -> 2.07 GHz);
#   P2 at the same D and L, dread is faster than sleep (schedutil boosts the frequency of tasks that wake
#      from I/O wait); then the read-back curve of Q3 (0.61 -> 1.90, 0.83 -> 3.23 GHz) follows from dread,
#      and the scan-phase curve (0.918 -> 3.31, 1.0 -> 3.45) from spin + short I/O waits;
#   P3 spin 3.48-3.50, wakers:1 ~3.36-3.40, wakers:3 ~3.11-3.20 (reproduces docs/7).
# Read-only on $BIG_ROOT/bench/fio.dat. SYSCONF is applied for the duration and restored (clock_state.py).
#   src/setup/detach.sh clock-m1 hardware/clock_m1.sh        (~45 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_clock_m1
LOADS="spin spin+wakers:1 spin+wakers:3 dread:0.6 dread:0.75 dread:0.9"
for d in 0.6 0.75 0.9 0.97; do for l in 0.1 0.3 1 3 13; do LOADS+=" sleep:$d:$l"; done; done
.venv/bin/python hardware/clock_state.py --out "$OUT" --loads $LOADS --reps 2 --dur 40 --read-file "$BIG_ROOT/bench/fio.dat"
echo "results: $OUT"
