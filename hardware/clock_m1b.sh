#!/usr/bin/env bash
# M1b: the high on-CPU end of the clock curve (D = 0.95-1), where Q6, Q1 and Q3's scan phase run. In M1
# (hardware/clock_m1.sh, results/bench/20261119-183008_clock_m1) the cells there scattered by +-0.15 GHz in 40 s,
# and with the curve from M1 alone Q6 x SF100 is predicted 2.6-3.5 % slow (interpolated 0.97 -> 1.0).
# M1 showed that at D = 0.97 the idle length does not matter (3.24-3.35 GHz for L = 0.1-13 ms), so L = 1 ms,
# with L = 13 ms at 0.95 / 0.98 as the check. 3 blocks, random order, 90 s per cell.
# Prediction: monotone, 0.95 ~3.30, 0.98 ~3.40, 0.99 ~3.43, spin ~3.46-3.50 (the fitted Q curve: 3.45 - 1.70 (1 - D)).
# SYSCONF applied for the duration and restored (clock_state.py).   src/setup/detach.sh clock-m1b hardware/clock_m1b.sh  (~35 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_clock_m1b
.venv/bin/python hardware/clock_state.py --out "$OUT" --reps 3 --dur 90 --read-file "$BIG_ROOT/bench/fio.dat" \
    --loads spin sleep:0.95:1 sleep:0.97:1 sleep:0.98:1 sleep:0.99:1 sleep:0.95:13 sleep:0.98:13
echo "results: $OUT"
