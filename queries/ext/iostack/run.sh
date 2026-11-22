#!/usr/bin/env bash
# E1 (docs/ext/iostack.md): one backend under io_method (sync / worker / io_uring) x debug_io_direct (off / data).
#   Q6 x SF100 cold: all 6 cells x 3 blocks; Q1 x SF100 cold (K is 7 %, the CPU-heavy contrast):
#   sync/off, worker/off, sync/data, io_uring/data x 2 blocks. Random cell order within each block.
# Each cell: ALTER SYSTEM io_method / debug_io_direct + restart (tpch.ext.iostack.server), then one cold run
# (label iostack_<method>_<direct>). On exit (also on error) both are RESET and PostgreSQL restarted.
# Predictions (model/ext/iostack/q{6,1}_sf100.json, params 8402a6f5657e) were written before; hashes below.
# turbostat over the whole job (the worker cells wake other cores).
#   src/setup/detach.sh iostack queries/ext/iostack/run.sh       (~3 h)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
export PYTHONPATH="$PWD/src"
PY=.venv/bin/python
[[ " $(./tpch sysconf show | tr -d '\n') " == *'"cpufreq"'* ]] || { echo "SYSCONF lacks cpufreq" >&2; exit 1; }
echo "== registered predictions (written before the runs)"
sha256sum model/params.json model/ext/iostack/q6_sf100.json model/ext/iostack/q1_sf100.json src/tpch/ext/iostack/*.py
TS="results/jobs/$(date +%Y%m%d-%H%M%S)_iostack_turbostat.txt"
sudo -n turbostat --quiet --interval 10 --show Time_Of_Day_Seconds,CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,CPU%c1,CPU%c6 \
  --out "$TS" </dev/null >/dev/null 2>&1 &
cleanup() {
  sudo -n pkill -f -- "turbostat .*--out $TS" 2>/dev/null || true
  $PY -m tpch.ext.iostack.server reset || true
}
trap cleanup EXIT
echo "== turbostat -> $TS"
cell() {   # query method direct block
  echo "== $1 $2/$3 block $4"
  $PY -m tpch.ext.iostack.server set "$2" "$3"
  ./tpch experiment --query "$1" --sf 100 --cache cold --mode plain --blocks 1 --label "iostack_$2_$3"
}
for b in 1 2 3; do
  for c in $(printf '%s\n' sync:off sync:data worker:off worker:data io_uring:off io_uring:data | shuf); do
    cell q6 "${c%%:*}" "${c#*:}" "$b"
  done
done
for b in 1 2; do
  for c in $(printf '%s\n' sync:off worker:off sync:data io_uring:data | shuf); do
    cell q1 "${c%%:*}" "${c#*:}" "$b"
  done
done
