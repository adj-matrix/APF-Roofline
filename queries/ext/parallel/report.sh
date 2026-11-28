#!/usr/bin/env bash
# Figures and numbers.json of ext/parallel: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report ext/parallel \
    --input m1=20261122-174727_parallel_m1 --input m2=20261122-185424_parallel_m2 \
    --input m6=20261123-022212_parallel_m6 --input m7=20261125-155744_parallel_m7 \
    --input 'p1_runs=20261122-18*_q6_sf100_plain_cold_parallel_{m}_n*' \
    --input 'p2_runs=20261122-19*_q1_sf100_plain_cold_parallel_{m}_n*' \
    "$@"
