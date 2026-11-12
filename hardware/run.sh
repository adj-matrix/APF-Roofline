#!/usr/bin/env bash
# Device characterization of kugenuma06's data volume (not tied to a query or SF).
# Rerun after loading or rewriting tables (their location changes) and on a new volume (sdb).
# Reads only, plus scratch files under $DATA_ROOT/bench.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

hardware/controller_cache_test.sh       # C1–C4: what the RAID controller cache holds
./tpch bench location --table 1:orders 1:customer 10:orders 10:customer
                                        # bandwidth vs position; evicts the controller cache first
./tpch bench mem                        # random-access latency vs working-set size (CPU only)
./tpch report hardware
# fio block-size sweep: ./tpch bench io (results in report #1, queries/q6/sf10)
