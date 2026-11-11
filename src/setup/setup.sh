#!/usr/bin/env bash
# Reproducible environment for the TPC-H experiments.
#
# Usage: src/setup/setup.sh [stage ...]        (no stage = all, in order)
#
#   sys      apt packages: build tools, sysstat, fio, numactl, perf, python venv
#   pg       PostgreSQL $PG_MAJOR from the PGDG apt repo (no distro "main" cluster)
#   py       .venv from requirements.txt; writes requirements.lock
#   dbgen    build TPC-H dbgen at the pinned commit
#   cluster  initdb a dedicated cluster in $PGDATA, apply tpch.conf, start it
#   data     for each SF in $TPCH_SFS: generate .tbl and load into database tpch_sf<N>
#
# Every stage is idempotent. Runs as a normal user with sudo, or as root
# (docker); as root the cluster is owned by the "postgres" OS user.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETUP="$REPO/src/setup"
# shellcheck source=../../config.env
source "$REPO/config.env"

if [[ $EUID -eq 0 ]]; then
  SUDO=()
  PG_OS_USER=${PG_OS_USER:-postgres}
else
  SUDO=(sudo)
  PG_OS_USER=${PG_OS_USER:-$(id -un)}
fi
# sudo resets the environment, so pass the non-interactive settings explicitly.
# Without them dpkg/ucf/needrestart open dialogs that hang a non-tty run forever.
NONINTERACTIVE=(env DEBIAN_FRONTEND=noninteractive UCF_FORCE_CONFFOLD=1 NEEDRESTART_MODE=a)
ROOT=("${SUDO[@]}" "${NONINTERACTIVE[@]}")
APT=("${ROOT[@]}" apt-get -y -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef)

log() { printf '\033[1;34m[setup %s]\033[0m %s\n' "${STAGE:-}" "$*"; }
die() { printf '\033[1;31m[setup %s] ERROR:\033[0m %s\n' "${STAGE:-}" "$*" >&2; exit 1; }

# Run a command as the OS user that owns the cluster.
as_pg() {
  if [[ "$(id -un)" == "$PG_OS_USER" ]]; then "$@"
  elif [[ $EUID -eq 0 ]]; then runuser -u "$PG_OS_USER" -- "$@"
  else sudo -u "$PG_OS_USER" -- "$@"; fi
}
pg() { as_pg "$PGBIN/psql" -X -q -v ON_ERROR_STOP=1 -h "$PGSOCK" -p "$PG_PORT" "$@"; }
pg_running() { as_pg "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; }

# ------------------------------------------------------------------------------
stage_sys() {
  "${APT[@]}" update -qq
  "${APT[@]}" install -qq --no-install-recommends \
    build-essential git curl ca-certificates lsb-release procps util-linux time \
    sysstat fio numactl python3-venv python3-pip
  # perf must match the running kernel. In docker the host kernel's package may not exist: best effort.
  "${APT[@]}" install -qq --no-install-recommends linux-tools-common "linux-tools-$(uname -r)" \
    || log "WARNING: perf for kernel $(uname -r) not installable; perf measurements unavailable"
}

stage_pg() {
  if [[ -x "$PGBIN/postgres" ]]; then
    log "PostgreSQL $PG_MAJOR already installed: $("$PGBIN/postgres" --version)"; return
  fi
  "${APT[@]}" install -qq --no-install-recommends postgresql-common
  # We run our own cluster; stop the package from creating and starting 'main'.
  local cc=/etc/postgresql-common/createcluster.conf
  if grep -qE '^\s*#?\s*create_main_cluster' "$cc"; then
    "${SUDO[@]}" sed -i -E 's/^\s*#?\s*create_main_cluster.*/create_main_cluster = false/' "$cc"
  else
    echo 'create_main_cluster = false' | "${SUDO[@]}" tee -a "$cc" >/dev/null
  fi
  "${ROOT[@]}" /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
  "${APT[@]}" install -qq --no-install-recommends "postgresql-$PG_MAJOR" "postgresql-client-$PG_MAJOR"
  log "installed: $("$PGBIN/postgres" --version)"
}

stage_py() {
  [[ -x "$REPO/.venv/bin/python" ]] || python3 -m venv "$REPO/.venv"
  "$REPO/.venv/bin/pip" install -q --upgrade pip
  "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
  "$REPO/.venv/bin/pip" freeze --exclude-editable > "$REPO/requirements.lock"
  log "python env ready: $("$REPO/.venv/bin/python" --version)"
}

DBGEN_DIR="$REPO/build/tpch-kit/dbgen"
stage_dbgen() {
  local kit="$REPO/build/tpch-kit"
  [[ -d "$kit/.git" ]] || git clone -q "$DBGEN_REPO" "$kit"
  if [[ "$(git -C "$kit" rev-parse HEAD)" != "$DBGEN_COMMIT" ]]; then
    git -C "$kit" fetch -q origin
    git -C "$kit" checkout -q "$DBGEN_COMMIT"
  fi
  make -s -C "$DBGEN_DIR" MACHINE=LINUX DATABASE=POSTGRESQL -j"$(nproc)" >/dev/null 2>&1 \
    || make -C "$DBGEN_DIR" MACHINE=LINUX DATABASE=POSTGRESQL   # rerun verbosely on failure
  [[ -x "$DBGEN_DIR/dbgen" ]] || die "dbgen build failed"
  log "dbgen ready at $DBGEN_DIR (commit ${DBGEN_COMMIT:0:12})"
}

stage_cluster() {
  [[ -x "$PGBIN/initdb" ]] || die "PostgreSQL $PG_MAJOR not installed; run the 'pg' stage"
  "${SUDO[@]}" mkdir -p "$DATA_ROOT"
  "${SUDO[@]}" chown "$PG_OS_USER": "$DATA_ROOT"
  as_pg mkdir -p "$PGSOCK" "$TBL_DIR"

  if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
    as_pg "$PGBIN/initdb" -D "$PGDATA" -E UTF8 --locale=C \
      --auth-local=trust --auth-host=scram-sha-256 >/dev/null
    log "initialized cluster at $PGDATA"
  fi
  local have; have=$(cat "$PGDATA/PG_VERSION")
  [[ "$have" == "$PG_MAJOR" ]] || die "$PGDATA is PostgreSQL $have, config wants $PG_MAJOR"

  # Render tpch.conf; restart only if it changed.
  local tmp; tmp=$(mktemp)
  sed -e "s|@PG_PORT@|$PG_PORT|" -e "s|@PGSOCK@|$PGSOCK|" "$SETUP/postgresql.tpch.conf" > "$tmp"
  if (( PG_MAJOR >= 18 )); then
    printf "\n# PG18+: synchronous I/O as the baseline (AIO variants are separate experiments)\nio_method = sync\n" >> "$tmp"
  fi
  local changed=0
  if ! as_pg cmp -s "$tmp" "$PGDATA/tpch.conf"; then
    as_pg tee "$PGDATA/tpch.conf" < "$tmp" >/dev/null; changed=1
  fi
  rm -f "$tmp"
  as_pg grep -q "^include_if_exists = 'tpch.conf'" "$PGDATA/postgresql.conf" \
    || echo "include_if_exists = 'tpch.conf'" | as_pg tee -a "$PGDATA/postgresql.conf" >/dev/null

  if ! pg_running; then
    as_pg "$PGBIN/pg_ctl" -D "$PGDATA" -l "$DATA_ROOT/postgres.log" -w start >/dev/null
    log "started PostgreSQL on socket $PGSOCK port $PG_PORT"
  elif (( changed )); then
    as_pg "$PGBIN/pg_ctl" -D "$PGDATA" -l "$DATA_ROOT/postgres.log" -w restart >/dev/null
    log "tpch.conf changed; restarted PostgreSQL"
  else
    log "PostgreSQL already running"
  fi
}

gen_sf() {  # generate $TBL_DIR/sf<N>/*.tbl*
  local sf=$1 out="$TBL_DIR/sf$1" jobs=$GEN_JOBS
  [[ -x "$DBGEN_DIR/dbgen" ]] || die "dbgen not built; run the 'dbgen' stage"
  if [[ -f "$out/.done" ]]; then log "SF$sf: tbl files already generated"; return; fi
  (( sf > 1 )) || jobs=1
  mkdir -p "$out"; rm -f "$out"/*.tbl*
  log "SF$sf: generating with $jobs job(s) into $out"
  (
    cd "$DBGEN_DIR"
    export DSS_PATH="$out" DSS_CONFIG="$DBGEN_DIR"
    if (( jobs == 1 )); then
      ./dbgen -f -s "$sf" >/dev/null
    else
      seq 1 "$jobs" | xargs -P "$jobs" -I{} ./dbgen -f -s "$sf" -C "$jobs" -S {} >/dev/null
      ./dbgen -f -s "$sf" -T l >/dev/null   # nation + region are not chunked
    fi
  )
  printf 'sf=%s jobs=%s dbgen_commit=%s\n' "$sf" "$jobs" "$DBGEN_COMMIT" > "$out/.done"
}

load_sf() {  # load $TBL_DIR/sf<N> into database tpch_sf<N>
  local sf=$1 db="tpch_sf$1" out="$TBL_DIR/sf$1"
  pg_running || die "PostgreSQL not running; run the 'cluster' stage"
  if [[ "$(pg -d "$db" -Atc "SELECT value FROM tpch_meta WHERE key='loaded'" 2>/dev/null || true)" == "true" ]]; then
    log "SF$sf: database $db already loaded"; return
  fi
  log "SF$sf: (re)creating database $db"
  pg -d postgres -c "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"
  pg -d "$db" -f "$SETUP/schema.sql"

  local f t
  for f in "$out"/*.tbl*; do
    t=$(basename "$f"); t=${t%%.tbl*}
    # dbgen ends each line with '|'; strip it so COPY sees the right column count.
    sed 's/|$//' "$f" | pg -d "$db" -c "\\copy $t FROM STDIN WITH (FORMAT text, DELIMITER '|')" &
    while (( $(jobs -rp | wc -l) >= GEN_JOBS )); do wait -n; done
  done
  while (( $(jobs -rp | wc -l) > 0 )); do wait -n; done

  log "SF$sf: primary keys"
  pg -d "$db" -f "$SETUP/keys.sql"
  # FREEZE sets hint bits / visibility map now, so the first measured scan does not write.
  log "SF$sf: VACUUM (FREEZE, ANALYZE)"
  pg -d "$db" -c "VACUUM (FREEZE, ANALYZE)"
  pg -d "$db" -c "CREATE TABLE tpch_meta (key text PRIMARY KEY, value text);
    INSERT INTO tpch_meta VALUES ('sf', '$sf'), ('dbgen_commit', '$DBGEN_COMMIT'),
      ('loaded_at', now()::text), ('loaded', 'true');"
  log "SF$sf: loaded; size $(pg -d "$db" -Atc "SELECT pg_size_pretty(pg_database_size('$db'))")"
}

stage_data() {
  local sf
  for sf in $TPCH_SFS; do gen_sf "$sf"; load_sf "$sf"; done
}

# ------------------------------------------------------------------------------
ALL=(sys pg py dbgen cluster data)
STAGES=("$@"); (( ${#STAGES[@]} )) || STAGES=("${ALL[@]}")
for STAGE in "${STAGES[@]}"; do
  declare -F "stage_$STAGE" >/dev/null || die "unknown stage '$STAGE' (stages: ${ALL[*]})"
  log "start"; "stage_$STAGE"; log "done"
done
