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
# SIZES: multipliers of that volume (bigger batch files at the same N, as at SF1000: Q3 x SF1000 / 64MB has
# 512 files per side of ~300 MB; x1 gives 13 MB). Tags: n<N>_r<rep>, or n<N>_x<M>_r<rep> for M != 1.
#   NS=512 SIZES="1 3 10" REPS=2 src/setup/detach.sh temp-spill-size hardware/temp_spill.sh   (~1.6 h, 68 GiB peak)
for rep in $(seq ${REPS:-2}); do
  for cell in $(for n in ${NS:-64 16 4}; do for m in ${SIZES:-1}; do echo "$n:$m"; done; done | shuf); do
    n=${cell%%:*} m=${cell#*:}
    tag="n${n}_r${rep}"; [[ $m == 1 ]] || tag="n${n}_x${m}_r${rep}"
    .venv/bin/python hardware/temp_spill.py "$BIG_ROOT/bench/spill" --out "$OUT" --tag "$tag" \
        --dev "$BIG_IO_DEVICE" --batches "$n" --inner $((1200 * m)) --outer $((5600 * m)) --rate 56 \
        --read-file "$BIG_ROOT/bench/fio.dat" --read-rate 450 --settle 5
  done
done
echo "results: $OUT"
