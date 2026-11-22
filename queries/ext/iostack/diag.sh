#!/usr/bin/env bash
# E1 fix, diagnosis of the io_uring user-cycle penalty (docs/ext/iostack.md, "Held-out results", item 3):
# Q1 SF100 spends 2-3.5 % more user cycles under io_uring than under sync with the same instructions.
# Q1 x {sync, io_uring} x {off, data}, one block, random order, with
#   - perf counting the backend's own thread only (PERF_NO_INHERIT=1: io-wq threads no longer counted in),
#   - user-mode instructions, L1d misses and LLC loads (= L2 misses) to see whether the extra cycles are misses,
#   - irq / softirq entries while the backend is on CPU (completions landing on its core).
# Expected before running: io_uring cells show more L1d / LLC loads per user instruction than sync, and io_uring /
# data about one hard irq per read on the backend (sync / data: ~0, the backend sleeps through its reads).
# Settings RESET on exit.     src/setup/detach.sh iostack-diag queries/ext/iostack/diag.sh      (~50 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
export PERF_NO_INHERIT=1
export PERF_EVENTS="task-clock,cycles,instructions,ref-cycles,cycles:u,instructions:u,L1-dcache-load-misses:u,LLC-loads:u,irq:irq_handler_entry,irq:softirq_entry,context-switches,cpu-migrations,page-faults,major-faults"
cleanup() { $PY -m tpch.ext.iostack.server reset || true; }
trap cleanup EXIT
for c in $(printf '%s\n' sync:off sync:data io_uring:off io_uring:data | shuf); do
  m=${c%%:*} d=${c#*:}
  echo "== q1 $m/$d"
  $PY -m tpch.ext.iostack.server set "$m" "$d"
  ./tpch experiment --query q1 --sf 100 --cache cold --mode plain --blocks 1 --label "iostack_diag_${m}_${d}"
done
