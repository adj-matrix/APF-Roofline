#!/usr/bin/env bash
# Figures of hardware/ (device, memory, clock): which measured results they are built from. Extra arguments are passed on (e.g. --out DIR).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
./tpch report hardware \
    --input scan=20261112-000731_location \
    --input earlier=20261111-162205_location,20261111-171016_location,20261111-184649_location \
    --input mem=20261112-000154_mem --input cache_test=20261111-184550_controller_cache \
    "$@"
PYTHONPATH=src:. .venv/bin/python -m hardware.clock_figures \
    --input cells=20261115-163808_clock_state,20261115-170918_clock_state,20261115-172629_clock_state \
    --input cells=20261119-183008_clock_m1,20261119-190937_clock_m1b \
    --input turbostat=results/jobs/20261119-152028_q1-sf1000_turbostat.txt \
    --input long_sf1000=20261119-152046_q1_sf1000_plain_cold_blind_r0 \
    --input long_sf100=20261119-171918_q1_sf100_plain_cold_blind_r0 \
    "$@"
