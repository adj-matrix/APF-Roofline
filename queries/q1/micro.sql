-- Q1 taken apart: each statement adds one piece of Q1 to the previous one (warm, SF1).
-- Run as queries `q1.<name>` (./tpch experiment --query q1.m_scan q1.m_filter ...).
-- Cost of a piece = cycles(statement) - cycles(the statement it extends).
-- name: m_scan
SELECT count(*) FROM lineitem;
-- name: m_filter
SELECT count(*) FROM lineitem WHERE l_shipdate <= DATE '1998-09-02';
-- name: m_group
SELECT l_returnflag, l_linestatus, count(*)
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
-- name: m_sum_qty
SELECT l_returnflag, l_linestatus, count(*), sum(l_quantity)
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
-- name: m_avg_qty
SELECT l_returnflag, l_linestatus, count(*), avg(l_quantity)
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
-- name: m_sum_avg_qty
SELECT l_returnflag, l_linestatus, count(*), sum(l_quantity), avg(l_quantity)
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
-- name: m_sum_price
SELECT l_returnflag, l_linestatus, count(*), sum(l_extendedprice)
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
-- name: m_disc_price
SELECT l_returnflag, l_linestatus, count(*), sum(l_extendedprice * (1 - l_discount))
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
-- name: m_charge
SELECT l_returnflag, l_linestatus, count(*), sum(l_extendedprice * (1 - l_discount) * (1 + l_tax))
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
-- name: m_avg_disc
SELECT l_returnflag, l_linestatus, count(*), avg(l_discount)
FROM lineitem WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus;
