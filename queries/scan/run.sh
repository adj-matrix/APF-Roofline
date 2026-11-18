#!/usr/bin/env bash
# Machine parameters for the model (src/tpch/model): kernel cycles per page read from sdb (cold) vs from the
# page cache (warm), and the clock of light vs heavy per-tuple code. SF100 database, not TPC-H queries.
#   orders (20 GiB, fits in the page cache): cold and warm, light and heavy, 5 blocks
#   lineitem (86 GiB): cold, heavy, 3 blocks
# Long job: src/setup/detach.sh scan-sdb queries/scan/run.sh   (~50 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
./tpch experiment --query scan.orders_light scan.orders_heavy --sf 100 --cache cold warm --mode plain --blocks 5 --label kpage
./tpch experiment --query scan.lineitem_heavy --sf 100 --cache cold --mode plain --blocks 3 --label kpage
