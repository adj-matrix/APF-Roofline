#!/usr/bin/env bash
# E1 fix, M7d: io_method = worker reproduced with PostgreSQL 18's dispatch (replica_pool.c) on lineitem's segments.
# The paced-readers replica spread the reads over all 3 workers (2.3-2.9 other cores awake -> 3.0-3.3 GHz); PG wakes
# the lowest idle worker first, so at Q1's rate ~1 core is awake (PG Q1 worker / off: 1.05 cores, 3.43 GHz).
# CB = page verification per read in the worker: dU / clock = 61 k / 3.3 GHz ~ 18 us (Q6), 76 k -> 23 us (Q1).
# Cells: Q6's (140 us) and Q1's (1000 us) CPU per read, buffered / direct, 3 workers; Q6 with 1 worker (held-out).
#   src/setup/detach.sh iostack-m7d queries/ext/iostack/m7d.sh     (~20 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_iostack_m7d
.venv/bin/python -m tpch.ext.iostack.replica --out "$OUT" --reps 2 --dur 80 --cells \
  pool:0:3:140:18 pool:1:3:140:18 pool:0:3:1000:23 pool:1:3:1000:23 pool:0:1:140:18 pool:1:1:140:18
echo "replicas: $OUT"
