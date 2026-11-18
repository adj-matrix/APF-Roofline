#!/usr/bin/env bash
# A copy of tpch_sf<N>_bigkeys whose order keys have SF1000's hash collisions, so their CPU cost can be
# calibrated at a small SF.
# PostgreSQL hashes a BIGINT as hash_uint32(lo32 ^ hi32) (hashint8), and hash_uint32 is a bijection: keys
# below 2^32 never collide, but at SF1000 (order keys up to 6e9) every key a >= 2^32 has a twin
# b = (a - 2^32) xor 1 -- also a valid order key, same low 5 bits -- with exactly the same 32-bit hash. 28.4 %
# of the keys are >= 2^32 and 28.4 % are their twins: a hash join probe finds a non-matching inner tuple with
# its own hash value for 56.8 % of the keys (when the twin passed the inner side's filter), and compares it.
# Here SF1000's key space is scaled down: keys above B = 2^32 x SF/1000 (rounded down to a multiple of 32)
# are moved up by 2^32 - B, so key k > B becomes k - B + 2^32 and its twin is (k - B) xor 1 < B: the same
# fraction of keys and twins as SF1000. Both tables are rewritten by ALTER ... USING (one sequential copy:
# the physical order of the rows, hence the probe order, stays as loaded).
#   src/setup/detach.sh variant-twinkeys src/setup/variant_twinkeys.sh 10      (SF10: ~15 GB, ~15 min)
# Used with TPCH_DB_VARIANT=twinkeys (src/tpch/config.py: dbname). Remove: DROP DATABASE tpch_sf<N>_twinkeys.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source config.env
SF=${1:?usage: variant_twinkeys.sh SF}
SRC=tpch_sf${SF}_bigkeys DST=tpch_sf${SF}_twinkeys
B=$(( (2**32 * SF / 1000) / 32 * 32 ))
SHIFT=$(( 2**32 - B ))
pg() { psql -h "$PGSOCK" -p "$PG_PORT" -v ON_ERROR_STOP=1 "$@"; }
[[ "$(pg -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$SRC'")" == 1 ]] \
  || { echo "$SRC missing: src/setup/variant_bigkeys.sh $SF" >&2; exit 1; }
if [[ "$(pg -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$DST'")" == 1 ]]; then
  echo "$DST exists; drop it first to rebuild" >&2; exit 1
fi
echo "== $DST from $SRC: keys > $B moved up by $SHIFT"
pg -d postgres -c "CREATE DATABASE $DST TEMPLATE $SRC TABLESPACE tpch_big"
pg -d "$DST" -c "\timing on" \
   -c "ALTER TABLE orders ALTER COLUMN o_orderkey TYPE bigint
         USING CASE WHEN o_orderkey > $B THEN o_orderkey + $SHIFT ELSE o_orderkey END" \
   -c "ALTER TABLE lineitem ALTER COLUMN l_orderkey TYPE bigint
         USING CASE WHEN l_orderkey > $B THEN l_orderkey + $SHIFT ELSE l_orderkey END" \
   -c "VACUUM (ANALYZE) orders" -c "VACUUM (ANALYZE) lineitem"
# check: keys moved, and the twins (pairs of order keys with one 32-bit hash; expected 2 x 28.4 % of orders)
pg -d "$DST" -Atc "SELECT 'orders', count(*), count(*) FILTER (WHERE o_orderkey >= 2^32), max(o_orderkey) FROM orders"
pg -d "$DST" -Atc "SELECT 'keys with a twin', count(*), round(count(*)::numeric / (SELECT count(*) FROM orders), 4)
    FROM orders a JOIN orders b ON hashint8(a.o_orderkey) = hashint8(b.o_orderkey) AND a.o_orderkey <> b.o_orderkey"
pg -d "$DST" -Atc "SELECT 'lineitem keys >= 2^32', count(*) FILTER (WHERE l_orderkey >= 2^32), count(*) FROM lineitem"
