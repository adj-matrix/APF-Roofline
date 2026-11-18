#!/usr/bin/env bash
# Q3 x SF1000 revision, step C1 (2): same number of batches as SF1000, at SF100.
# The SF1000 / SF100 (64MB) profile comparison (queries/q3/sf1000/profile.sh) puts the largest excess in the
# scan phase on the entry instructions (pushes) of heapgettup_pagemode, 141x the SF100 samples: a store
# stall, presumably the store buffer filling behind tuple copies into the 512 batch buffers (8 KiB each)
# instead of 64. SF100 at work_mem 8MB has SF1000's 512/16 batches: if its profile shows the same stall,
# the cost is per spilled tuple and grows with the number of batches; if not, it grows with the SF itself.
# Store-side counters replace the LLC events (4 general counters per hyperthread):
#   resource_stalls.sb                     cycles stalled because the store buffer is full
#   l2_rqsts.rfo_miss                      stores (RFOs) that missed L2
#   dtlb_store_misses.miss_causes_a_walk   stores that missed the STLB (page walk)
#   src/setup/detach.sh q3-profile-nbatch queries/q3/sf1000/profile_nbatch.sh      (~20 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
export PERF_RECORD_HZ=199
export PERF_EVENTS=task-clock,cycles,instructions,ref-cycles,cycles:u,resource_stalls.sb,l2_rqsts.rfo_miss,dtlb_store_misses.miss_causes_a_walk,context-switches,cpu-migrations,page-faults,major-faults
for wm in $(printf '%s\n' 8MB 64MB | shuf); do
  ./tpch experiment --query q3 --sf 100 --cache cold --mode plain --blocks 1 --label "profile_wm$wm" \
    --set work_mem="$wm" --set enable_nestloop=off
done
