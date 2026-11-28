#!/usr/bin/env bash
# Figures and numbers.json of q3/sf10: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report q3/sf10 \
    --cond cal=20261112-001314_q3_cal,20261112-001332_q3_cal \
    --cond main=20261112-001426_q3_main,20261112-002730_q3_main \
    --input q6=20261111-165703_q6_scale3,20261111-170742_q6_scale3 \
    --input scan=20261112-000731_location --input mem=20261112-000154_mem \
    "$@"
