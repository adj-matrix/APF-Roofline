#!/usr/bin/env bash
# E2 (parallel), M4 (docs/ext/parallel.md): replica points for O_DIRECT near the ridge, before PostgreSQL's P3.
#   Q6's work (450k): psync and io_uring direct, P = 2, 3, 4      Q1's work (3000k): psync and io_uring direct, P = 6, 8
# 60 s each (direct: no page cache, steady rate). Over the job: turbostat, procmon, iostat on sdb.
#   src/setup/detach.sh parallel-m4 queries/ext/parallel/m4.sh      (~12 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
OUT=results/bench/$(date +%Y%m%d-%H%M%S)_parallel_m4
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
  par:psync:1:2:450:60 par:psync:1:3:450:60 par:psync:1:4:450:60 par:uring:1:2:450:60 par:uring:1:3:450:60 par:uring:1:4:450:60 \
  par:psync:1:6:3000:60 par:psync:1:8:3000:60 par:uring:1:6:3000:60 par:uring:1:8:3000:60
echo "== done $OUT"
