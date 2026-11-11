#!/usr/bin/env bash
# Q6 × SF1, together with SF10 in the same experiment: does the SF10 model scale down to SF1?
# Prediction and hypotheses: prediction.md (written before the first experiment).
#
# Design: randomized blocks (src/tpch/experiment.py). Each experiment evicts the RAID
# controller's write-back cache before its first cold run.
# History: label scale  = experiment 1, before eviction existed (SF1 cold hit the controller cache)
#          label scale2 = first experiment with eviction
#          label scale3 = rerun after renaming session/round -> experiment/block (main)
#
# Precondition: lineitem of tpch_sf1 and tpch_sf10 both rewritten by VACUUM FULL
# (then VACUUM (FREEZE, ANALYZE)), so both have the same layout treatment.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
LABEL=${LABEL:-scale3}

gov=$(sort -u /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor)
[[ "$gov" == schedutil ]] || { echo "governor is '$gov', expected schedutil (plan §4.11)" >&2; exit 1; }

# 5 blocks × {SF1, SF10} × {cold, warm}, shuffled within each block
./tpch experiment --query q6 --sf 1 10 --cache cold warm --mode plain   --blocks 5 --label "$LABEL"
# per-node times: 1 block × {SF1, SF10} × {cold, warm}
./tpch experiment --query q6 --sf 1 10 --cache cold warm --mode analyze --blocks 1 --label "$LABEL"
# Bandwidth at each table's location: hardware/run.sh (device characterization, not part of this cell).
