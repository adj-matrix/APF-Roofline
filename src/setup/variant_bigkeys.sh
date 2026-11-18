#!/usr/bin/env bash
# A copy of tpch_sf<N> with BIGINT order keys, as SF >= 358 has them (setup.sh: bigkeys), so the CPU cost of
# 8-byte keys can be calibrated at a small SF. o_orderkey and l_orderkey are rewritten in place: same column
# order, same types as a fresh BIGINT load. The copy lives in tablespace tpch_big (sdb), not on the root
# file system; temp files stay where tpch_sf<N>'s go (pg_default), so both copies spill to the same place.
#   src/setup/detach.sh variant-bigkeys src/setup/variant_bigkeys.sh 10      (SF10: ~15 GB, ~10 min)
# Used with TPCH_DB_VARIANT=bigkeys (src/tpch/config.py: dbname). Remove: DROP DATABASE tpch_sf<N>_bigkeys.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source config.env
SF=${1:?usage: variant_bigkeys.sh SF}
SRC=tpch_sf$SF DST=tpch_sf${SF}_bigkeys
pg() { psql -h "$PGSOCK" -p "$PG_PORT" -v ON_ERROR_STOP=1 "$@"; }
if [[ "$(pg -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$DST'")" != 1 ]]; then
  echo "== $DST from $SRC"
  pg -d postgres -c "CREATE DATABASE $DST TEMPLATE $SRC TABLESPACE tpch_big"
fi
pg -d "$DST" -c "\timing on" \
   -c "ALTER TABLE orders ALTER COLUMN o_orderkey TYPE bigint" \
   -c "ALTER TABLE lineitem ALTER COLUMN l_orderkey TYPE bigint" \
   -c "VACUUM (ANALYZE) orders" -c "VACUUM (ANALYZE) lineitem"
for db in "$SRC" "$DST"; do
  pg -d "$db" -Atc "SELECT '$db', format_type(atttypid, atttypmod), attrelid::regclass, attname,
      pg_relation_size(attrelid) / 8192 AS pages FROM pg_attribute
      WHERE attname IN ('o_orderkey', 'l_orderkey') ORDER BY 3"
done
