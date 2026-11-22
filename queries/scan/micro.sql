-- Scan-only statements on TPC-H tables (not TPC-H queries): machine parameters of the model
-- (kernel cycles per page read from sdb vs from the page cache; clock vs code). Run as scan.<name>.
-- name: orders_light
-- one Seq Scan, ~nothing per tuple: page handling dominates. (count(o_comment), not count(*): the 2026-11-17
-- runs used count(*), which the planner answers with an Index Only Scan of orders_pkey; those runs are unused.)
SELECT count(o_comment) FROM orders;
-- name: orders_heavy
-- one Seq Scan with numeric arithmetic per tuple (CPU-bound on sdb, like Q1)
SELECT sum(o_totalprice * (1 + o_shippriority) * 1.01) FROM orders;
-- name: lineitem_heavy
-- Q1's most expensive expression over all of lineitem (CPU-bound on sdb; the table does not fit in RAM)
SELECT sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) FROM lineitem;
-- name: lineitem_light
-- one Seq Scan of all of lineitem, ~nothing per tuple: PostgreSQL's own I/O path at the device's rate, independent
-- of any TPC-H query (E1 fix M8: the I/O workers' CPU per read, reclaim). count(l_comment): not answerable from an index.
SELECT count(l_comment) FROM lineitem;
-- name: lineitem_min
-- as lineitem_light with less work per tuple: count of the 5th column (l_quantity) deforms 5 attributes instead of 16
-- (E1 fix M10, the low point of the work-per-read family). Not l_orderkey: the planner answers that from lineitem_pkey.
SELECT count(l_quantity) FROM lineitem;
-- name: lineitem_mid
-- as lineitem_light with more work per tuple: numeric arithmetic on two columns (E1 fix M10, the high point)
SELECT sum(l_extendedprice * (1 - l_discount)) FROM lineitem;
