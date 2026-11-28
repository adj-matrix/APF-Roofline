#!/usr/bin/env bash
# Figures and numbers.json of q1/sf10: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report q1/sf10 \
    --cond micro=20261112-004121_q1_micro \
    --cond main=20261112-004816_q1_main,20261112-010850_q1_main \
    --input q6=20261111-165703_q6_scale3,20261111-170742_q6_scale3 \
    --input q3=20261112-001426_q3_main,20261112-002730_q3_main \
    --input scan=20261112-000731_location \
    "$@"
