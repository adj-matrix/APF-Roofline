#!/usr/bin/env bash
# Figures and numbers.json of ext/attribution: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report ext/attribution \
    --input a1=20261123-140514_attribution_a1 --input a1_period=20000003 \
    --input 'a1_runs=20261123-1[34]*_{q}_sf100_plain_cold_attribution_n{n}_r0' \
    --input a2=20261123-144847_attribution_a2 --input a2_period=2000003 \
    --input 'a2_runs=20261123-1[45]*_sf100_plain_cold_attribution_a2_r0' \
    "$@"
