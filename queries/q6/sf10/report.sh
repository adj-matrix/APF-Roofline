#!/usr/bin/env bash
# Figures and numbers.json of q6/sf10: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report q6/sf10 \
    --cond A=v1 --cond B1=v2defrag --cond B2=condB --cond C=condC \
    --input fio=20261106-164953_io,20261106-180540_io,20261106-180959_io \
    --timeline A B2 C \
    "$@"
