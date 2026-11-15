#!/usr/bin/env bash
# Temp-file spill microbenchmark on the big volume (Q3 × SF100 Part D): read-back rate of hash-join batch
# files vs the number of batches, and how writeback disturbs a concurrent table scan.
# Files only under $BIG_ROOT/bench/spill (our own volume), deleted at the end. No system settings changed.
#   src/setup/detach.sh temp-spill hardware/temp_spill.sh        (≈ 25 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_temp_spill
mkdir -p "$OUT"
# Q3 × SF100 at 64MB wrote 2.4 GiB inner + 11.4 GiB outer at ~56 MiB/s while scanning at ~450 MiB/s.
# Half the volume, same rate, so each file gets the same amount per 30 s writeback period.
for rep in $(seq ${REPS:-2}); do
  for n in $(printf "%s\n" ${NS:-64 16 4} | shuf); do
    .venv/bin/python hardware/temp_spill.py "$BIG_ROOT/bench/spill" --out "$OUT" --tag "n${n}_r${rep}" \
        --dev "$BIG_IO_DEVICE" --batches "$n" --inner 1200 --outer 5600 --rate 56 \
        --read-file "$BIG_ROOT/bench/fio.dat" --read-rate 450 --settle 5
  done
done
echo "results: $OUT"
