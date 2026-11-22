#!/usr/bin/env bash
# E2 (parallel), M8 (docs/ext/parallel.md): does a shared queue with 3 readers (io_method = worker) let two
# leapfrogging participants escape the slow leapfrog rate when they are in phase? leapq.c, 20 s cells, 2 reps:
#   buffered and O_DIRECT, phase 0 / 16 / 32 / 48 MiB, 3 readers with own fds (as PostgreSQL's I/O workers);
#   buffered phase 0 with 1 reader, and with 3 readers sharing one set of fds.
# Over the job: iostat on sdb. sysconf applied and restored by the driver.
#   src/setup/detach.sh parallel-m8 queries/ext/parallel/m8.sh      (~12 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m8
mkdir -p "$OUT"
S_TIME_FORMAT=ISO iostat -x -t -y 1 sdb > "$OUT/iostat.log" &
IO=$!
trap 'kill $IO 2>/dev/null || true' EXIT
echo "== out $OUT"
sha256sum src/tpch/ext/parallel/*.py src/tpch/ext/parallel/*.c
$PY -m tpch.ext.parallel.bench --out "$OUT" --reps 2 --cells \
  leapq:0:0:3:20 leapq:0:16:3:20 leapq:0:32:3:20 leapq:0:48:3:20 \
  leapq:1:0:3:20 leapq:1:16:3:20 leapq:1:32:3:20 leapq:1:48:3:20 \
  leapq:0:0:1:20 leapq:0:0:3:20:0
echo "== done $OUT"
