#!/usr/bin/env bash
# Q3 x SF1000 revision, step 1: CPU cost of BIGINT order keys (training data, SF10 only).
# The blind Q3 x SF1000 runs used 15 % more user-mode cycles than predicted (instructions +7 %, IPC -7 %
# against 10 x the SF100 control). SF1000 has BIGINT o_orderkey / l_orderkey, the SF10 scale model INTEGER.
# Here the scale model (q3.sf100_shape at SF10, warm) runs on both key types, interleaved in one session:
# tpch_sf10 (INTEGER) and tpch_sf10_bigkeys (BIGINT, src/setup/variant_bigkeys.sh). The ratio of the two
# at each work_mem gives the key-width factor for the scale-model cycles and for the cost per spilled tuple.
#   src/setup/detach.sh q3-cal-bigkeys queries/q3/sf1000/cal_bigkeys.sh      (~30 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
source config.env
[[ "$(psql -h "$PGSOCK" -p "$PG_PORT" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='tpch_sf10_bigkeys'")" == 1 ]] \
  || { echo "tpch_sf10_bigkeys missing: src/setup/variant_bigkeys.sh 10" >&2; exit 1; }
WMS=(64MB 128MB 1GB)
cell() {  # keys work_mem mode
  local v=; [[ $1 == big ]] && v=bigkeys
  TPCH_DB_VARIANT=$v ./tpch experiment --query q3.sf100_shape --sf 10 --cache warm --mode "$3" --blocks 1 \
      --set join_collapse_limit=1 --set work_mem="$2" --label "calk_$1_wm$2"
}
for b in 1 2 3; do
  for c in $(for k in int big; do for wm in "${WMS[@]}"; do echo "$k:$wm"; done; done | shuf); do
    cell "${c%%:*}" "${c#*:}" plain
  done
done
for k in int big; do for wm in "${WMS[@]}"; do cell "$k" "$wm" analyze; done; done
