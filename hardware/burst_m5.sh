#!/usr/bin/env bash
# M5 (docs/8 §7): the scan's extra wait while hash-join batch files are written back (param
# burst_wait_per_burst_s, fitted on Q3 x SF100). hardware/temp_spill.sh already ran a 450 MiB/s reader (the
# table scan) during the writes for N = 4-512 (results/bench/2026111{5,8}-*_temp_spill): its read-call time
# rises with N by the same amounts as the fitted parameter (vs N = 16: +0.11 / +0.42 / +0.48 s per second of
# burst at N = 64 / 256 / 512; fitted +0.09 / +0.41 / +0.61). Missing: the reader alone, without writes,
# which gives the absolute level. Cells (random order): reader only for 121 s (= the write time of a x1 cell)
# x3, and N = 16 / 512 x2 as in temp_spill.sh to tie this session to the earlier ones.
# Prediction (from the fitted value at N = 16, 0.33 s per burst-second x 1.88 s/GiB x 6.64 GiB = 4.1 s):
# reader alone ~59 s of read-call time; N = 16 ~63.4 s, N = 512 ~69.4 s (the earlier runs).
# Files only under $BIG_ROOT/bench/spill (our own volume), deleted at the end; reads our own fio.dat.
# No system settings changed.   src/setup/detach.sh burst-m5 hardware/burst_m5.sh     (~30 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_burst_m5
mkdir -p "$OUT"
for cell in $(printf '%s\n' r0:1 r0:2 r0:3 n16:1 n16:2 n512:1 n512:2 | shuf); do
  kind=${cell%%:*} rep=${cell#*:}
  echo "== ${kind}_r$rep"
  if [[ $kind == r0 ]]; then
    .venv/bin/python hardware/temp_spill.py "$BIG_ROOT/bench/spill" --out "$OUT" --tag "r0_r$rep" --dev "$BIG_IO_DEVICE" \
        --batches 1 --read-file "$BIG_ROOT/bench/fio.dat" --read-rate 450 --reader-only 121
  else
    .venv/bin/python hardware/temp_spill.py "$BIG_ROOT/bench/spill" --out "$OUT" --tag "${kind}_r$rep" \
        --dev "$BIG_IO_DEVICE" --batches "${kind#n}" --inner 1200 --outer 5600 --rate 56 \
        --read-file "$BIG_ROOT/bench/fio.dat" --read-rate 450 --settle 5
  fi
  sleep 10
done
echo "results: $OUT"
