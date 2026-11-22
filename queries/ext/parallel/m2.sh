#!/usr/bin/env bash
# E2 (parallel), M2 (docs/ext/parallel.md): why P interleaved streams give less than one (M1). No work per read
# (pure I/O), 30 s per cell, lineitem's segments of tpch_sf100 (read-only), PostgreSQL's chunking:
#   O_DIRECT psync (QD 1 per stream, no kernel read-ahead): P = 1, 2, 3, 4, 6, 8 -> the controller alone
#   O_DIRECT io_uring QD 16 per stream: P = 1, 2, 4, 8                       -> more in flight per stream
#   buffered psync (kernel read-ahead 128 KiB on this LV): P = 1, 2, 4, 8    -> the kernel's read-ahead on top
# Over the job: iostat -x on sdb (1 s: request size, await, queue size), turbostat, procmon.
#   src/setup/detach.sh parallel-m2 queries/ext/parallel/m2.sh      (~15 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m2
mkdir -p "$OUT"
TS="$OUT/turbostat.txt"
sudo -n turbostat --quiet --interval 2 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CPU%c1,CPU%c6,CoreTmp,PkgTmp,PkgWatt \
  --out "$TS" </dev/null >/dev/null 2>&1 &
S_TIME_FORMAT=ISO iostat -x -t -y 1 sdb > "$OUT/iostat.log" &
IO=$!
$PY -m tpch.procmon --out "$OUT/procmon.jsonl" &
PM=$!
cleanup() {
  sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true
  kill "$PM" "$IO" 2>/dev/null || true
}
trap cleanup EXIT
echo "== out $OUT"
sha256sum src/tpch/ext/parallel/*.py src/tpch/ext/parallel/*.c
$PY -m tpch.ext.parallel.bench --out "$OUT" --cells \
  par:psync:1:1:0:30 par:psync:1:2:0:30 par:psync:1:3:0:30 par:psync:1:4:0:30 par:psync:1:6:0:30 par:psync:1:8:0:30 \
  par:uring:1:1:0:30 par:uring:1:2:0:30 par:uring:1:4:0:30 par:uring:1:8:0:30 \
  par:psync:0:1:0:30 par:psync:0:2:0:30 par:psync:0:4:0:30 par:psync:0:8:0:30
echo "== done $OUT"
