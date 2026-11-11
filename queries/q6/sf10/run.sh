#!/usr/bin/env bash
# Report #1 experiments: Q6 at SF10, isolating disk layout and CPU frequency governor.
#
#   A  fragmented lineitem (8 parallel COPY streams)  + schedutil   label v1   (run before this script)
#   B  lineitem rewritten by VACUUM FULL               + schedutil   label condB
#   C  lineitem rewritten by VACUUM FULL               + performance label condC
#
# Precondition: lineitem already rewritten (VACUUM (FULL) lineitem; VACUUM (FREEZE, ANALYZE) lineitem).
# Also repeats the fio sweep twice under the same governor as the first sweep.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

GOV=/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
ORIG=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)
set_gov() { echo "$1" | sudo -n tee $GOV >/dev/null; echo "[exp] governor -> $(sort -u $GOV | tr '\n' ' ')"; }
trap 'set_gov "$ORIG"' EXIT

q6_set() {  # label
  ./tpch run --query q6 --sf 10 --cache cold --mode plain   --repeat 3 --label "$1"
  ./tpch run --query q6 --sf 10 --cache warm --mode plain   --repeat 3 --label "$1"
  ./tpch run --query q6 --sf 10 --cache cold --mode analyze --repeat 1 --label "$1"
  ./tpch run --query q6 --sf 10 --cache warm --mode analyze --repeat 1 --label "$1"
}

set_gov schedutil
./tpch bench io
./tpch bench io
q6_set condB

set_gov performance
q6_set condC
