-- TPC-H Q1, validation parameter: DELTA=90 (1998-12-01 - 90 days = 1998-09-02).
-- Bound written as a DATE literal, as in Q6: `DATE - INTERVAL` would yield a timestamp
-- and force a per-row date->timestamp cast in the filter.
SELECT l_returnflag,
       l_linestatus,
       sum(l_quantity)                                        AS sum_qty,
       sum(l_extendedprice)                                   AS sum_base_price,
       sum(l_extendedprice * (1 - l_discount))                AS sum_disc_price,
       sum(l_extendedprice * (1 - l_discount) * (1 + l_tax))  AS sum_charge,
       avg(l_quantity)                                        AS avg_qty,
       avg(l_extendedprice)                                   AS avg_price,
       avg(l_discount)                                        AS avg_disc,
       count(*)                                               AS count_order
FROM lineitem
WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus;
