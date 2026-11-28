#!/usr/bin/env bash
# Figures and numbers.json of ext/cachesim: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report ext/cachesim \
    --input m1=20261123-151131_cachesim_m1 --input m3=20261123-170821_cachesim_m3 \
    --input 'q3_runs=20261123-1[5-7]*_q3_sf100_plain_cold_cachesim_*_r0' \
    "$@"
