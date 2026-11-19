#!/usr/bin/env bash
# M4 (docs/8-model-framework.md): batch read-back with CPU work between reads, on the big volume.
# Layouts separate the number of batches N from the file size (rule D3): N = 16 / 64 / 512 at the x1
# volume (6.8 GiB: 1200 MiB inner + 5600 MiB outer), and N = 512 at x3 (files 3x as large).
# Per layout: one write, then one read pass per CPU cost (0-90 us per 8 KiB block; Q3's phase 3 spends
# ~55-80 us per block), see hardware/readback_cpu.py.
# Files only under $BIG_ROOT/bench/spill (our own volume), deleted at the end. No system settings changed.
#   src/setup/detach.sh readback-cpu hardware/readback_cpu.sh        (~50 min, 20 GiB peak)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_readback_cpu
mkdir -p "$OUT"
for cell in $(printf '%s\n' 16:1 64:1 512:1 512:3 | shuf); do
  n=${cell%%:*} m=${cell#*:}
  tag="n${n}_x${m}"
  echo "== $tag"
  .venv/bin/python -m hardware.readback_cpu "$BIG_ROOT/bench/spill" --out "$OUT" --tag "$tag" \
      --dev "$BIG_IO_DEVICE" --batches "$n" --inner $((1200 * m)) --outer $((5600 * m)) --rate 56
done
echo "results: $OUT"
