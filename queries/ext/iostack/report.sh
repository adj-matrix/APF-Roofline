#!/usr/bin/env bash
# Figures and numbers.json of ext/iostack: which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
./tpch report ext/iostack \
    --input 'runs=2026111*_{q}_sf100_plain_cold_iostack_*' \
    --input 'heldout_runs=20261120-03*_{q}_sf100_plain_cold_iostack_*' \
    --input m7=20261120-002725_iostack_m7,20261120-014441_iostack_m7b,20261120-022430_iostack_m7c \
    --input m7d=20261120-024700_iostack_m7d --input m9=20261122-155305_iostack_m9 \
    --input 'scans=2026112[2]-*_scan.lineitem_*_sf100_plain_cold_iostack_m*' \
    --input 'q6_same_period=20261122-17*_q6_sf100_plain_cold_iostack_m11_worker_off_r0' \
    "$@"
