#!/usr/bin/env bash
# Device characterization of kugenuma06 (not tied to a query or SF).
#
#   hardware/run.sh        # data volume DATA_ROOT on sda (SF1, SF10)       ≈ 10 min
#   hardware/run.sh big    # big volume BIG_ROOT on sdb (SF >= BIG_SF_MIN)  ≈ 30 min: run it detached,
#                          #   src/setup/detach.sh hw-sdb hardware/run.sh big
#
# Rerun after loading or rewriting tables (their location changes) and on a new volume.
# Reads only, plus scratch files under <root>/bench (on sdb also the 16 GiB fio file: our own volume).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env

case ${1:-data} in
data)
  hardware/controller_cache_test.sh     # C1–C5: what the RAID controller cache holds
  ./tpch bench location --table 1:orders 1:customer 10:orders 10:customer
                                        # bandwidth vs position; evicts the controller cache first
  ./tpch bench mem                      # random-access latency vs working-set size (CPU only)
  echo "figures: put the new run ids into hardware/report.sh, then run it"
  # fio block-size sweep: ./tpch bench io (results in report #1, queries/q6/sf10)
  ;;
big)
  ./tpch bench io --volume big          # lays out $BIG_ROOT/bench/fio.dat (16 GiB) + block-size sweep
  VOLUME=big hardware/controller_cache_test.sh
  # the SF100 tables Q6/Q3/Q1 read, and the fio file for comparison (each file timed on its own device)
  ./tpch bench location --no-default --file "$BIG_ROOT/bench/fio.dat" \
      --table 100:lineitem 100:orders 100:customer
  ;;
*) echo "usage: hardware/run.sh [data|big]" >&2; exit 2 ;;
esac
