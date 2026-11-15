-- Q3 variants, run as queries `q3.<name>` (./tpch experiment --query q3.<name> ...).
-- name: sf100_shape
-- Q3 with SF100's join order, (lineitem ⋈ orders) ⋈ customer, for a scale model at SF10.
-- Needs --set join_collapse_limit=1 (otherwise the planner reorders it into SF10's own plan).
SELECT l_orderkey,
       sum(l_extendedprice * (1 - l_discount)) AS revenue,
       o_orderdate,
       o_shippriority
FROM (lineitem JOIN orders ON l_orderkey = o_orderkey)
     JOIN customer ON c_custkey = o_custkey
WHERE c_mktsegment = 'BUILDING'
  AND o_orderdate < DATE '1995-03-15'
  AND l_shipdate  > DATE '1995-03-15'
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10;
