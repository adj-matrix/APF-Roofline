#!/usr/bin/env bash
# E1 fix, M7b: the replica clocks at the operating points of the E1 cells, 120 s per cell instead of 25 s.
# M7 (25 s) gave clocks 0.2-0.7 GHz above the SF100 runs at the same on-CPU share (sync / data, io_uring / off)
# and readers clocks scattered by +-0.2 GHz; question: does the clock fall with run length (as at SF1000,
# docs/8 sec. 6), or is the replica missing something?  Prediction if run length is the cause: psync direct
# 200 us -> ~2.1 GHz (PG Q6 sync/data), readers buffered 950 -> ~3.06 (PG Q6 worker/off).
#   src/setup/detach.sh iostack-m7b queries/ext/iostack/m7b.sh     (~40 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_iostack_m7b
.venv/bin/python -m tpch.ext.iostack.replica --out "$OUT" --reps 2 --dur 120 --cells spin \
  io:psync:1:1:150 io:psync:1:1:200 io:psync:1:1:250 io:psync:1:1:1000 io:uring:1:16:140 \
  readers:direct:1000 readers:buffered:950 readers:buffered:150
echo "replicas: $OUT"
