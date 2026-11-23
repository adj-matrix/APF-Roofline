#!/usr/bin/env bash
# E4 (page cache), M2 (docs/ext/cachesim.md): held-out test of the measured page-cache capacity. Q3 x SF100,
# work_mem 64MB, cold, with and without a 24 GiB mlocked balloon on node 0 (src/tpch/ext/cachesim/balloon.c, run as
# root under numactl --membind=0), 2 blocks, order shuffled within a block. Predictions registered before the run:
# model/ext/cachesim/heldout_balloon.json (balloon: 13,468 MiB read back from the device, 355.1 s; none: 10,591 MiB,
# 351.2 s). Per-node meminfo sampled over the job. The balloon is always released on exit.
#   src/setup/detach.sh cachesim-m2 queries/ext/cachesim/m2.sh      (~30 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_cachesim_m2
mkdir -p "$OUT"
cc -O2 -Wall -o "$OUT/balloon" src/tpch/ext/cachesim/balloon.c
echo "== out $OUT"
echo "== registered predictions (written before the runs)"
sha256sum model/ext/cachesim/heldout_balloon.json model/ext/cachesim/params.json
( while :; do
    ts=$(date +%s.%N)
    for n in 0 1; do
      awk -v ts="$ts" -v n="$n" '/MemFree|FilePages|Active\(file\)|Inactive\(file\)|Mlocked/ {gsub(":","",$3); printf "%s,%s,%s,%s\n", ts, n, $3, $4}' \
        /sys/devices/system/node/node$n/meminfo
    done
    sleep 1
  done ) > "$OUT/nodes.csv" &
SAMP=$!
BAL=""
# kill the balloon itself (by exact name): a signal to the sudo wrapper is not relayed to it (2026-11-23 run hung here)
release() { [[ -n "$BAL" ]] && { sudo -n pkill -x balloon 2>/dev/null || true; wait "$BAL" 2>/dev/null || true; BAL=""; }; return 0; }
cleanup() { release; sudo -n pkill -x balloon 2>/dev/null || true; kill "$SAMP" 2>/dev/null || true; }
trap cleanup EXIT
for b in 1 2; do
  for c in $(printf '%s\n' balloon none | shuf); do
    echo "== q3 64MB $c (block $b)"
    if [[ $c == balloon ]]; then
      sudo -n numactl --membind=0 "$OUT/balloon" 24 > "$OUT/balloon_$b.out" &
      BAL=$!
      until grep -q ready "$OUT/balloon_$b.out" 2>/dev/null; do sleep 1; done
      grep -E "MemFree|Mlocked" /sys/devices/system/node/node0/meminfo
    fi
    ./tpch experiment --query q3 --sf 100 --cache cold --mode plain --blocks 1 --set work_mem=64MB --label "cachesim_$c"
    release
  done
done
echo "== done $OUT"
