#!/usr/bin/env bash
# E2 (parallel), M7 (docs/ext/parallel.md): the controlled two-stream probes, as a script. On 2026-11-23 they were
# run by hand in the foreground (no SYSCONF, ~3 min); this script reproduces them as run (also without SYSCONF).
#   two continuous sequential streams at a fixed gap: 8, 16, 32, 48, 64, 96, 128 MiB, buffered (cold) and O_DIRECT
#   leapfrog like a parallel scan's two participants (64 MiB chunks, each stream skips the other's), phase 0 and
#   32 MiB, O_DIRECT and buffered (cold), twice each
# 8-10 s per cell, lineitem's segments read-only (src/tpch/ext/parallel/twostream.c).
#   src/setup/detach.sh parallel-m7 queries/ext/parallel/m7.sh      (~6 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m7
mkdir -p "$OUT"
cc -O2 -Wall -o "$OUT/twostream" src/tpch/ext/parallel/twostream.c
$PY - "$OUT/lineitem.list" <<'PY'
import sys
from pathlib import Path
from tpch.config import load
from tpch.ext.parallel.bench import lineitem_list
lineitem_list(load(), Path(sys.argv[1]))
PY
cold() { sudo -n sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'; sleep 2; }
run() { numactl --cpunodebind=0 --membind=0 "$OUT/twostream" "@$OUT/lineitem.list" "$@" | tee -a "$OUT/cells.jsonl"; }
echo "== out $OUT"
for g in 8 16 32 48 64 96 128; do cold; run "$g" 0 8; done
for g in 8 16 32 48 64 96; do run "$g" 1 8; done
for g in 0 32; do run "$g" 1 8 4 64; done
for g in 0 32 0 32; do cold; run "$g" 0 10 4 64; done
echo "== done $OUT"
