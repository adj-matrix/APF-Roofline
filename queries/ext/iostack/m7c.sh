#!/usr/bin/env bash
# E1 fix, M7c: the replicas on lineitem's own segment files (tpch_sf100, read-only): 86 x 1 GiB files read one
# after the other (readahead restarts per file), 86 GiB > RAM (buffered reads reclaim), the table's on-disk layout.
# Question: the 16 GiB-file replica predicts Q6 io_uring / off at 102 s, PostgreSQL took 121 s with the backend
# ~10 % idle; does the real file set make the replica wait / slow down too? Also sync / off (the base, 142 s),
# sync / data, io_uring / data on the same files, and buffered io_uring at Q1's CPU per read.
#   src/setup/detach.sh iostack-m7c queries/ext/iostack/m7c.sh     (~25 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_iostack_m7c
.venv/bin/python -m tpch.ext.iostack.replica --out "$OUT" --reps 2 --dur 120 --cells \
  li-io:uring:0:16:140 li-io:uring:0:16:1000 li-io:psync:0:1:140 li-io:psync:1:1:200 li-io:uring:1:16:140
echo "replicas: $OUT"
