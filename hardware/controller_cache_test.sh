#!/usr/bin/env bash
# The RAID controller's write-back cache (PERC H710P), found in Q6 × SF1: what does it hold?
# Writes only scratch files under $DATA_ROOT/bench (deleted at the end); the fio benchmark file
# is only read. All reads are O_DIRECT (bypass the OS page cache), 128 KiB like PostgreSQL.
#
# Claims under test:
#   C1 written data is served from the controller cache (>> disk rate)
#   C2 reads do not allocate in it (reading other data twice stays at disk rate)
#   C3 reads do not evict it (the written file is still fast after reading other data)
#   C4 new writes evict it (after writing 2 GiB elsewhere, the file reads at disk rate)
#
# Output: results/bench/<stamp>_controller_cache/log.txt (dd reports decimal MB/s)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
A=$DATA_ROOT/bench/cachetest_a.tmp
B=$DATA_ROOT/bench/cachetest_b.tmp
OTHER=$DATA_ROOT/bench/fio.dat                  # written once long ago; read only here
N=7035                                          # 7035 × 128 KiB = 879 MiB (= SF1 lineitem)
OUT=$RESULTS_DIR/bench/$(date +%Y%m%d-%H%M%S)_controller_cache
mkdir -p "$OUT"
exec > >(tee "$OUT/log.txt") 2>&1
trap 'rm -f "$A" "$B"' EXIT

rd() { printf '%-58s' "$1"; dd if="$2" of=/dev/null bs=128k count=$N iflag=direct 2>&1 | tail -1 | sed 's/.*copied, //'; }

dd if=/dev/urandom of="$A" bs=128k count=$N oflag=direct conv=fsync status=none
echo "wrote 879 MiB scratch file A"
rd "C1  read A (just written)"                       "$A"
rd "C1  read A again"                                "$A"
rd "C2  read other data (fio file, first 879 MiB)"   "$OTHER"
rd "C2  read the same other data again"              "$OTHER"
rd "C3  read A after reading other data"             "$A"
dd if=/dev/zero of="$B" bs=1M count=2048 oflag=direct conv=fsync status=none; rm -f "$B"
echo "wrote and deleted 2 GiB scratch file B; waiting 15 s for destage"; sleep 15
rd "C4  read A after 2 GiB of other writes"          "$A"
rd "C4  read A again"                                "$A"
