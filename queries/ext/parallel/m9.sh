#!/usr/bin/env bash
# E2 (parallel), M9 (docs/ext/parallel.md): batch submission. Two leapfrogging participants in phase, 3 readers with own fds;
# each submits its reads BATCH at a time (PostgreSQL read stream batch mode). leapq.c, 20 s cells, 2 reps:
#   buffered and O_DIRECT, BATCH = 1, 2, 4, 8, 16.
# Over the job: iostat on sdb. sysconf applied and restored by the driver.
#   src/setup/detach.sh parallel-m9 queries/ext/parallel/m9.sh      (~10 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m9
mkdir -p "$OUT"
S_TIME_FORMAT=ISO iostat -x -t -y 1 sdb > "$OUT/iostat.log" &
IO=$!
trap 'kill $IO 2>/dev/null || true' EXIT
echo "== out $OUT"
sha256sum src/tpch/ext/parallel/*.py src/tpch/ext/parallel/*.c
$PY -m tpch.ext.parallel.bench --out "$OUT" --reps 2 --cells \
  leapq:0:0:3:20:1:1 leapq:0:0:3:20:1:2 leapq:0:0:3:20:1:4 leapq:0:0:3:20:1:8 leapq:0:0:3:20:1:16 leapq:1:0:3:20:1:1 leapq:1:0:3:20:1:2 leapq:1:0:3:20:1:4 leapq:1:0:3:20:1:8 leapq:1:0:3:20:1:16
echo "== done $OUT"
