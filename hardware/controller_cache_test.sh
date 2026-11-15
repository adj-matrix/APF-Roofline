#!/usr/bin/env bash
# The RAID controller's write-back cache (PERC H710P), found in Q6 × SF1: what does it hold?
# Writes only scratch files under <root>/bench (deleted at the end); the fio benchmark file
# is only read. All reads are O_DIRECT (bypass the OS page cache), 128 KiB like PostgreSQL.
#
#   hardware/controller_cache_test.sh            # volume DATA_ROOT (sda)
#   VOLUME=big hardware/controller_cache_test.sh # volume BIG_ROOT (sdb); needs ./tpch bench io --volume big first
#
# Claims under test:
#   C1 written data is served from the controller cache (>> disk rate)
#   C2 reads do not allocate in it (reading other data twice stays at disk rate)
#   C3 reads do not evict it (the written file is still fast after reading other data)
#   C5 writes to the OTHER virtual disk (sda <-> sdb, same controller) evict it too: one shared cache
#   C4 new writes evict it (after writing 2 GiB elsewhere on this volume, the file reads at disk rate)
#
# Output: results/bench/<stamp>_controller_cache[_big]/log.txt (dd reports decimal MB/s)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
source config.env
VOLUME=${VOLUME:-data}
case $VOLUME in
  data) ROOT=$DATA_ROOT; DEV=$IO_DEVICE; OTHER_ROOT=$BIG_ROOT; SUFFIX= ;;
  big)  ROOT=$BIG_ROOT;  DEV=$BIG_IO_DEVICE; OTHER_ROOT=$DATA_ROOT; SUFFIX=_big ;;
  *) echo "VOLUME must be data or big" >&2; exit 2 ;;
esac
A=$ROOT/bench/cachetest_a.tmp
B=$ROOT/bench/cachetest_b.tmp
X=$OTHER_ROOT/bench/cachetest_x.tmp             # C5: a write on the other virtual disk
OTHER=$ROOT/bench/fio.dat                       # written once by ./tpch bench io; read only here
N=7035                                          # 7035 × 128 KiB = 879 MiB (= SF1 lineitem)
[[ -f $OTHER ]] || { echo "$OTHER missing: run ./tpch bench io${SUFFIX:+ --volume big} first" >&2; exit 1; }
OUT=$RESULTS_DIR/bench/$(date +%Y%m%d-%H%M%S)_controller_cache$SUFFIX
mkdir -p "$OUT"
exec > >(tee "$OUT/log.txt") 2>&1
trap 'rm -f "$A" "$B" "$X"' EXIT
echo "volume $VOLUME: $ROOT on $DEV ($(cat /sys/class/block/$DEV/device/scsi_disk/*/cache_type 2>/dev/null))"

rd() { printf '%-58s' "$1"; dd if="$2" of=/dev/null bs=128k count=$N iflag=direct 2>&1 | tail -1 | sed 's/.*copied, //'; }

dd if=/dev/urandom of="$A" bs=128k count=$N oflag=direct conv=fsync status=none
echo "wrote 879 MiB scratch file A"
rd "C1  read A (just written)"                       "$A"
rd "C1  read A again"                                "$A"
rd "C2  read other data (fio file, first 879 MiB)"   "$OTHER"
rd "C2  read the same other data again"              "$OTHER"
rd "C3  read A after reading other data"             "$A"
if [[ -d $OTHER_ROOT ]]; then
  mkdir -p "$OTHER_ROOT/bench"
  dd if=/dev/zero of="$X" bs=1M count=2048 oflag=direct conv=fsync status=none; rm -f "$X"
  echo "wrote and deleted 2 GiB on the other volume ($OTHER_ROOT); waiting 15 s for destage"; sleep 15
  rd "C5  read A after 2 GiB of writes on the other volume" "$A"
fi
dd if=/dev/zero of="$B" bs=1M count=2048 oflag=direct conv=fsync status=none; rm -f "$B"
echo "wrote and deleted 2 GiB scratch file B; waiting 15 s for destage"; sleep 15
rd "C4  read A after 2 GiB of other writes"          "$A"
rd "C4  read A again"                                "$A"
