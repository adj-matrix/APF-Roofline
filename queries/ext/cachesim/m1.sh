#!/usr/bin/env bash
# E4 (page cache), M1 (docs/ext/cachesim.md): where the page cache of a NUMA-bound process lives, and how much of
# what it wrote earlier it keeps. No TPC-H query. Each cell starts cold (sync; drop_caches = 3).
#   place_bind    numactl --cpunodebind=0 --membind=0: read 45 GiB of lineitem's segments (dd, 1 MiB, read-only)
#   place_nobind  numactl --cpunodebind=0 only: the same
#   keep_R        numactl --cpunodebind=0 --membind=0: write 8 GiB to our own file in BIG_ROOT/bench, sync, read R GiB
#                 of lineitem (R = 8, 16, 24, 32), then count the written file's pages still cached (fincore)
# Per-node page cache (FilePages, Active / Inactive(file), MemFree) sampled every second over the job.
#   src/setup/detach.sh cachesim-m1 queries/ext/cachesim/m1.sh      (~20 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
BIG=$($PY -c "from tpch.config import load; print(load().BIG_ROOT)")
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_cachesim_m1
mkdir -p "$OUT"
W="$BIG/bench/cachesim_w.dat"
$PY - "$OUT/lineitem.list" <<'PY'
import sys
from pathlib import Path
from tpch.config import load
from tpch.ext.parallel.bench import lineitem_list
lineitem_list(load(), Path(sys.argv[1]))
PY
( while :; do
    ts=$(date +%s.%N)
    for n in 0 1; do
      awk -v ts="$ts" -v n="$n" '/MemFree|FilePages|Active\(file\)|Inactive\(file\)/ {gsub(":","",$3); printf "%s,%s,%s,%s\n", ts, n, $3, $4}' \
        /sys/devices/system/node/node$n/meminfo
    done
    sleep 1
  done ) > "$OUT/nodes.csv" &
SAMP=$!
cleanup() { kill "$SAMP" 2>/dev/null || true; rm -f "$W"; }
trap cleanup EXIT
cold() { sudo -n sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'; sleep 3; }
readg() {   # bind GiB: read the first GiB 1 GiB segments
  local bind=$1 g=$2
  head -n "$g" "$OUT/lineitem.list" | while read -r f; do
    numactl --cpunodebind=0 $bind dd if="$f" of=/dev/null bs=1M status=none
  done
}
mark() { echo "$(date +%s.%N),$1" >> "$OUT/marks.csv"; echo "== $1"; }
echo "== out $OUT"
for c in place_bind place_nobind; do
  cold; mark "$c start"
  if [[ $c == place_bind ]]; then readg "--membind=0" 45; else readg "" 45; fi
  mark "$c end"
done
for R in 8 16 24 32; do
  cold; mark "keep_$R start"
  numactl --cpunodebind=0 --membind=0 dd if=/dev/zero of="$W" bs=1M count=8192 status=none
  sync
  mark "keep_$R written"
  readg "--membind=0" "$R"
  mark "keep_$R read"
  fincore -b -n -o RES,SIZE "$W" | awk -v R="$R" '{print R","$1","$2}' >> "$OUT/keep.csv"
  cat "$OUT/keep.csv" | tail -1
  rm -f "$W"
done
cold
echo "== done $OUT"
