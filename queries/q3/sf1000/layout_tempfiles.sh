#!/usr/bin/env bash
# M4 follow-up (docs/8, read-back wait): the on-disk layout of Q3's hash-join batch files.
# hardware/readback_cpu.sh found that the read-back's off-CPU time does not depend on the CPU work between
# reads; it is a set of long waits (>= 2 ms), ~24 ms per physical discontinuity of the files in read
# order (4 layouts, 95-4073 discontinuities). If the same holds for PostgreSQL, Q3's phase-3 wait is
# extents x 24 ms. This run counts the extents of the temp files while Q3 x SF100 runs: every 5 s,
# `filefrag` (FIEMAP, read-only; without -s, so it does not force writeback and does not change the
# layout) over the files in pgsql_tmp of the tpch_big tablespace -> <job>_layout.csv
# (t, files, bytes, extents). The snapshot at the start of phase 3 gives the count.
# Settings as the read-back calibration (cal_readback.sh): 8MB / 16MB / 64MB, one run each, cold.
#   src/setup/detach.sh q3-layout queries/q3/sf1000/layout_tempfiles.sh      (~25 min)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
TMP=$(sudo -n sh -c 'ls -d /srv/tpch-sdb/pg/PG_18_*/pgsql_tmp')
OUT="results/jobs/$(date +%Y%m%d-%H%M%S)_q3-layout_layout.csv"
echo "t_epoch,files,bytes,extents" > "$OUT"
sample() {
  while true; do
    sudo -n sh -c 'cd "$1" && files=$(find . -type f) && [ -n "$files" ] &&
        { echo "$files" | xargs stat -c %s | awk "{s+=\$1} END {printf \"%d,%d,\", NR, s}";
          echo "$files" | xargs filefrag | awk "{e+=\$(NF-2)} END {print e}"; }' sh "$TMP" \
      | sed "s/^/$(date +%s),/" >> "$OUT" || true
    sleep 5
  done
}
sample &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT
echo "== layout samples -> $OUT"
for wm in $(printf '%s\n' 8MB 16MB 64MB | shuf); do
  ./tpch experiment --query q3 --sf 100 --cache cold --mode plain --blocks 1 --label "layout_wm$wm" --set work_mem="$wm"
done
