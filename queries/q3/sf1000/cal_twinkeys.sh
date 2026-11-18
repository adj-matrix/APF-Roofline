#!/usr/bin/env bash
# Q3 x SF1000 revision, step C1 (3): CPU cost of the hash collisions of BIGINT order keys >= 2^32 (training
# data, SF10 only). The SF1000 / SF100 profiles put the read-back phase's excess in the functions a hash
# join runs only when a probe's 32-bit hash equals an inner tuple's (ExecStoreMinimalTuple, deforming,
# int8eq): at SF1000 every order key >= 2^32 shares its hash with a twin key (src/setup/variant_twinkeys.sh).
# Here the scale model (q3.sf100_shape at SF10, warm) runs on BIGINT keys without collisions
# (tpch_sf10_bigkeys) and with SF1000's collisions (tpch_sf10_twinkeys), interleaved in one session; the
# difference in user-mode cycles over the number of false matches is the cost per false match.
#   src/setup/detach.sh q3-cal-twinkeys queries/q3/sf1000/cal_twinkeys.sh      (~30 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
source config.env
for db in tpch_sf10_bigkeys tpch_sf10_twinkeys; do
  [[ "$(psql -h "$PGSOCK" -p "$PG_PORT" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$db'")" == 1 ]] \
    || { echo "$db missing (src/setup/variant_*.sh 10)" >&2; exit 1; }
done
WMS=(64MB 128MB 1GB)
cell() {  # keys work_mem mode
  TPCH_DB_VARIANT=${1}keys ./tpch experiment --query q3.sf100_shape --sf 10 --cache warm --mode "$3" --blocks 1 \
      --set join_collapse_limit=1 --set work_mem="$2" --label "calt_$1_wm$2"
}
for b in 1 2 3; do
  for c in $(for k in big twin; do for wm in "${WMS[@]}"; do echo "$k:$wm"; done; done | shuf); do
    cell "${c%%:*}" "${c#*:}" plain
  done
done
for k in big twin; do for wm in "${WMS[@]}"; do cell "$k" "$wm" analyze; done; done
