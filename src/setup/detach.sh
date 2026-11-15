#!/usr/bin/env bash
# Run a long job on the server independently of the Claude Code / ssh session that starts it.
#
#   src/setup/detach.sh NAME COMMAND [ARGS...]
#   e.g. src/setup/detach.sh sf1000-load env TPCH_SFS=1000 src/setup/setup.sh data
#        src/setup/detach.sh q6-sf100 queries/q6/sf100/run.sh
#
# Why: a background process started from an interactive session (`setsid nohup ... &` included)
# is killed when that session's tool process cleans up its children. The first SF100 load died that
# way (2026-11-13 01:23). Every load and every multi-hour experiment goes through this script.
#
# The job runs as a transient systemd service (unit tpch-NAME, removed when it ends: no persistent
# change) as the current user, in the repo. Files: results/jobs/<stamp>_NAME.sh (exactly what runs;
# systemd would expand $VAR in an inline command, so the command goes into this script) and
# results/jobs/<stamp>_NAME.log (timestamped output, last line "EXIT <status>").
# A job whose unit is still running is not started twice.
#   status:  systemctl status tpch-NAME        log: tail -f results/jobs/<stamp>_NAME.log
#   stop:    sudo systemctl stop tpch-NAME     (SIGINT: measuring commands restore SYSCONF;
#                                               after a hard kill: ./tpch sysconf restore)
# PostgreSQL restarted inside a job gets its own scope (src/tpch/cache.py), so it outlives the job.
set -euo pipefail
(( $# >= 2 )) || { sed -n '2,6p' "$0"; exit 2; }
NAME=$1; shift
[[ $NAME =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad job name: $NAME" >&2; exit 2; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT="tpch-$NAME"
if systemctl is-active --quiet "$UNIT"; then
  echo "$UNIT is still running (systemctl status $UNIT)" >&2; exit 1
fi

mkdir -p "$REPO/results/jobs"
BASE="$REPO/results/jobs/$(date +%Y%m%d-%H%M%S)_$NAME"
LOG="$BASE.log"
q() { printf '%q' "$1"; }
{
  echo '#!/usr/bin/env bash'
  echo "# job $NAME, unit $UNIT, written $(date '+%F %T')"
  echo "cd $(q "$REPO")"
  echo "$(printf '%q ' "$@") 2>&1 | awk '{print strftime(\"%F %T\"), \$0; fflush()}' >> $(q "$LOG")"
  echo 'st=${PIPESTATUS[0]}'
  echo "echo \"\$(date '+%F %T') EXIT \$st\" >> $(q "$LOG")"
} > "$BASE.sh"
{ echo "# job $NAME, unit $UNIT, started $(date '+%F %T')"; echo "# script: $BASE.sh"; } > "$LOG"

sudo systemd-run --unit="$UNIT" --uid="$(id -u)" --gid="$(id -g)" --collect --quiet \
  --property=WorkingDirectory="$REPO" --setenv=HOME="$HOME" --setenv=PATH="$PATH" \
  --setenv=PYTHONUNBUFFERED=1 \
  --property=KillSignal=SIGINT bash "$BASE.sh"

sleep 1
systemctl is-active --quiet "$UNIT" || grep -q " EXIT " "$LOG" \
  || { echo "$UNIT did not start; see journalctl -u $UNIT" >&2; exit 1; }
echo "started $UNIT"
echo "  log:    $LOG"
echo "  status: systemctl status $UNIT"
