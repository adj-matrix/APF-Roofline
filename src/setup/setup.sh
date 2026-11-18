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

big() { (( $1 >= BIG_SF_MIN )); }
# The largest o_orderkey is 6,000,000 x SF (sparse keys), beyond INTEGER above SF 357: the order keys
# become BIGINT there (SF1000). All other keys fit INTEGER (largest: p_partkey = 200,000 x SF).
# BIGKEYS=1 forces BIGINT at any SF (to test this path on a small SF).
bigkeys() { [[ ${BIGKEYS:-0} == 1 ]] || (( $1 * 6000000 > 2147483647 )); }
# Delete a table's .tbl files once its COPY has committed (SF1000 does not fit twice on sdb);
# KEEP_TBL=0/1 overrides. Loaded tables are marked <tbl dir>/.loaded_<table>; a rerun resumes after them.
keep_tbl() { (( ${KEEP_TBL:-$( (( $1 >= 1000 )) && echo 0 || echo 1)} )); }
tbl_dir() { if big "$1"; then echo "$BIG_ROOT/tbl/sf$1"; else echo "$TBL_DIR/sf$1"; fi; }

gen_sf() {  # generate $(tbl_dir N)/*.tbl*
  local sf=$1 out; out=$(tbl_dir "$1"); local jobs=$GEN_JOBS
  [[ -x "$DBGEN_DIR/dbgen" ]] || die "dbgen not built; run the 'dbgen' stage"
  if [[ -f "$out/.done" ]]; then log "SF$sf: tbl files already generated"; return; fi
  (( sf > 1 )) || jobs=1
  mkdir -p "$out"
  if big "$sf"; then   # peak: all .tbl (~1.0 x SF GiB) + the tables being loaded (~1.0 x SF GiB)
    local free_gib; free_gib=$(( $(df -B1G --output=avail "$out" | tail -1) ))
    (( free_gib >= 2 * sf )) || die "SF$sf needs ~$(( 2 * sf )) GiB free on $out, only $free_gib GiB"
  fi
  rm -f "$out"/*.tbl* "$out"/.loaded_*
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

load_sf() {  # load $(tbl_dir N) into database tpch_sf<N>
  local sf=$1 db="tpch_sf$1" out; out=$(tbl_dir "$1")
  pg_running || die "PostgreSQL not running; run the 'cluster' stage"
  if [[ "$(pg -d "$db" -Atc "SELECT value FROM tpch_meta WHERE key='loaded'" 2>/dev/null || true)" == "true" ]]; then
    log "SF$sf: database $db already loaded"; return
  fi
  if compgen -G "$out/.loaded_*" >/dev/null \
     && [[ "$(pg -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$db'")" == 1 ]]; then
    log "SF$sf: resuming $db; already loaded: $(cd "$out" && echo .loaded_* | sed 's/\.loaded_//g')"
  else
  log "SF$sf: (re)creating database $db"
  pg -d postgres -c "DROP DATABASE IF EXISTS $db"
  if big "$sf"; then
    as_pg mkdir -p "$BIG_ROOT/pg"
    [[ "$(pg -d postgres -Atc "SELECT 1 FROM pg_tablespace WHERE spcname='tpch_big'")" == 1 ]] \
      || pg -d postgres -c "CREATE TABLESPACE tpch_big LOCATION '$BIG_ROOT/pg'"
    # Temp files (sorts for the PK builds, hash spills at query time) go there too, not to the root LV.
    pg -d postgres -c "CREATE DATABASE $db TABLESPACE tpch_big" \
                   -c "ALTER DATABASE $db SET temp_tablespaces = 'tpch_big'"
  else
    pg -d postgres -c "CREATE DATABASE $db"
  fi
  if bigkeys "$sf"; then
    log "SF$sf: o_orderkey, l_orderkey as BIGINT (largest order key $(( sf * 6000000 )))"
    sed -E 's/^(  [ol]_orderkey +)INTEGER /\1BIGINT  /' "$SETUP/schema.sql" | pg -d "$db"
  else
    pg -d "$db" -f "$SETUP/schema.sql"
  fi
  fi

  local f t tables
  if (( LOAD_JOBS == 1 )); then
    # One table at a time; its chunks concatenated in order into one COPY. TRUNCATE in the same
    # transaction lets COPY skip WAL (wal_level=minimal) and FREEZE the tuples.
    tables=$( { for f in "$out"/*.tbl* "$out"/.loaded_*; do
                  [[ -e $f ]] || continue
                  t=$(basename "$f"); t=${t#.loaded_}; echo "${t%%.tbl*}"
                done; } | sort -u)
    for t in $tables; do
      if [[ -f "$out/.loaded_$t" ]]; then log "SF$sf: $t already loaded"; continue; fi
      compgen -G "$out/$t.tbl*" >/dev/null || die "SF$sf: no .tbl files for $t and it is not loaded"
      log "SF$sf: loading $t"
      # dbgen ends each line with '|'; strip it so COPY sees the right column count.
      mapfile -t files < <(ls "$out/$t".tbl* | sort -V)
      sed 's/|$//' "${files[@]}" | pg -d "$db" -c "BEGIN" -c "TRUNCATE $t" \
        -c "\\copy $t FROM STDIN WITH (FORMAT text, DELIMITER '|', FREEZE)" -c "COMMIT"
      touch "$out/.loaded_$t"
      if ! keep_tbl "$sf"; then rm -f "$out/$t".tbl*; log "SF$sf: $t loaded, its .tbl files deleted"; fi
    done
  else
    for f in "$out"/*.tbl*; do
      t=$(basename "$f"); t=${t%%.tbl*}
      sed 's/|$//' "$f" | pg -d "$db" -c "\\copy $t FROM STDIN WITH (FORMAT text, DELIMITER '|')" &
      while (( $(jobs -rp | wc -l) >= LOAD_JOBS )); do wait -n; done
    done
    while (( $(jobs -rp | wc -l) > 0 )); do wait -n; done
  fi

  log "SF$sf: primary keys"
  # one statement at a time, skipping keys that exist (a resumed load may have built some)
  local stmt kt
  while IFS= read -r stmt; do
    kt=$(sed -E 's/^ALTER TABLE ([a-z]+) .*/\1/' <<<"$stmt")
    if [[ "$(pg -d "$db" -Atc "SELECT 1 FROM pg_constraint WHERE conrelid='$kt'::regclass AND contype='p'")" == 1 ]]; then
      log "SF$sf: $kt primary key exists"; continue
    fi
    log "SF$sf: $kt primary key"
    pg -d "$db" -c "$stmt"
  done < <(grep '^ALTER TABLE' "$SETUP/keys.sql")
  # FREEZE sets hint bits / visibility map now, so the first measured scan does not write.
  log "SF$sf: VACUUM (FREEZE, ANALYZE)"
  pg -d "$db" -c "VACUUM (FREEZE, ANALYZE)"
  pg -d "$db" -c "CREATE TABLE IF NOT EXISTS tpch_meta (key text PRIMARY KEY, value text); DELETE FROM tpch_meta;
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
