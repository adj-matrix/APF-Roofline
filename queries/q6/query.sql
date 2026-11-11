-- TPC-H Q6, validation parameters: DATE=1994-01-01, DISCOUNT=0.06, QUANTITY=24.
-- Upper bound written as a DATE literal: `DATE + INTERVAL` would yield a timestamp
-- and force a per-row date->timestamp cast in the filter.
SELECT sum(l_extendedprice * l_discount) AS revenue
FROM lineitem
WHERE l_shipdate >= DATE '1994-01-01'
  AND l_shipdate <  DATE '1995-01-01'
  AND l_discount BETWEEN 0.05 AND 0.07
  AND l_quantity < 24;
