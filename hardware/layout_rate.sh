#!/usr/bin/env bash
# Extent sizes of interleaved, slowly written files (M4 follow-up, docs/8): which on-disk extents does ext4
# give a file that grows by r x ~30 s per writeback (dirty_expire 30 s) when that is below mballoc's 8 MiB
# preallocation window? Q3 x SF1000 writes ~0.1 (N = 512) / 0.2 (N = 256) MiB/s per batch file, files of
# ~300 MiB. Here 64 files of 40 MiB at 0.1 and 0.2 MiB/s per file; layout only (hardware/readback_cpu.py
# --cpu-us ''), files deleted at the end. No system settings changed.
#   src/setup/detach.sh layout-rate hardware/layout_rate.sh      (~12 min, 2.5 GiB)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_layout_rate
mkdir -p "$OUT"
for rate in 6.4 12.8; do
  .venv/bin/python -m hardware.readback_cpu "$BIG_ROOT/bench/spill" --out "$OUT" --tag "n64_r${rate}" \
      --dev "$BIG_IO_DEVICE" --batches 64 --inner 0 --outer 2560 --rate "$rate" --cpu-us ''
done
echo "results: $OUT"
