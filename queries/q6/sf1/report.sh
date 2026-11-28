#!/usr/bin/env bash
# Figures and numbers.json of q6/sf1: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report q6/sf1 \
    --cond first=20261111-155321_q6_scale,20261111-160316_q6_scale \
    --cond main=20261111-165703_q6_scale3,20261111-170742_q6_scale3 \
    --cond repro=20261111-160906_q6_scale2,20261111-161827_q6_scale2 \
    --input scan=20261111-184649_location \
    "$@"
