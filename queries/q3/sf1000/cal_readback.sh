#!/usr/bin/env bash
# Q3 x SF1000 revision, step 2: the batch read-back regime of SF1000, reproduced at SF100 (training data).
# In the blind Q3 x SF1000 runs the read-back phase ran at 2.54 GHz (on-CPU share 0.62-0.67), the scan phase
# at 3.27 GHz; at SF100 / 64MB the read-back phase stays at ~3.3 GHz (on-CPU 0.83). The model's clock curve was
# trained on d >= 0.92 only, and the temp read-back / writeback-burst waits on <= 64 batches.
# work_mem 8MB gives SF100 SF1000's 512 batches for orders (16 for customer), 16MB 256; 64MB (64 batches) is
# the same-session reference. Cycles and on-CPU time are sampled per second (PERF_INTERVAL_MS), so clock,
# user and kernel cycles are known per phase; turbostat adds package power and temperature.
# Also tests whether the CPU cost per spilled tuple grows with the number of batches (SF10 scale model: 4).
#   src/setup/detach.sh q3-cal-readback queries/q3/sf1000/cal_readback.sh      (~1.5 h)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
TS="results/jobs/$(date +%Y%m%d-%H%M%S)_q3-cal-readback_turbostat.txt"
sudo -n turbostat --quiet --interval 10 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,CPU%c1,CPU%c6 \
  --out "$TS" </dev/null >/dev/null 2>&1 &
# kill turbostat itself: sudo ignores signals from its own process group (see queries/q3/sf1000/run.sh)
trap 'sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true' EXIT
echo "== turbostat -> $TS"
WMS=(8MB 16MB 64MB)
for b in 1 2 3; do
  for wm in $(printf '%s\n' "${WMS[@]}" | shuf); do
    ./tpch experiment --query q3 --sf 100 --cache cold --mode plain --blocks 1 --set work_mem="$wm" \
        --label "rb_wm$wm"
  done
done
for wm in 8MB 16MB; do
  ./tpch experiment --query q3 --sf 100 --cache cold --mode analyze --blocks 1 --set work_mem="$wm" --label "rb_wm$wm"
done
