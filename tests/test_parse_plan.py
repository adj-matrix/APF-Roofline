from tpch.parse.plan import parse_plan

# Shape of PostgreSQL EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) for a hash join under an aggregate.
EXPLAIN = [{
    "Plan": {
        "Node Type": "Aggregate", "Strategy": "Plain",
        "Actual Startup Time": 90.0, "Actual Total Time": 100.0, "Actual Rows": 1, "Actual Loops": 1,
        "Shared Hit Blocks": 10, "Shared Read Blocks": 1000, "Shared I/O Read Time": 40.0,
        "Plans": [{
            "Node Type": "Hash Join", "Join Type": "Inner", "Hash Cond": "(a.k = b.k)",
            "Actual Startup Time": 5.0, "Actual Total Time": 80.0, "Actual Rows": 500, "Actual Loops": 1,
            "Shared Hit Blocks": 10, "Shared Read Blocks": 1000, "Shared I/O Read Time": 40.0,
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "a",
                 "Actual Total Time": 50.0, "Actual Rows": 900, "Actual Loops": 1,
                 "Rows Removed by Filter": 100, "Filter": "(x < 5)",
                 "Shared Hit Blocks": 0, "Shared Read Blocks": 900, "Shared I/O Read Time": 35.0},
                {"Node Type": "Hash",
                 "Actual Total Time": 4.0, "Actual Rows": 50, "Actual Loops": 1,
                 "Shared Hit Blocks": 10, "Shared Read Blocks": 100, "Shared I/O Read Time": 5.0,
                 "Plans": [{"Node Type": "Seq Scan", "Relation Name": "b",
                            "Actual Total Time": 3.0, "Actual Rows": 50, "Actual Loops": 1,
                            "Shared Hit Blocks": 10, "Shared Read Blocks": 100,
                            "Shared I/O Read Time": 5.0}]},
            ],
        }],
    },
    "Planning Time": 0.5,
    "Execution Time": 101.0,
}]


def test_tree_shape_preorder():
    p = parse_plan(EXPLAIN)
    assert [n["type"] for n in p["nodes"]] == ["Aggregate", "Hash Join", "Seq Scan", "Hash", "Seq Scan"]
    assert [n["parent"] for n in p["nodes"]] == [None, 0, 1, 1, 3]
    assert [n["depth"] for n in p["nodes"]] == [0, 1, 2, 2, 3]
    assert p["planning_ms"] == 0.5 and p["execution_ms"] == 101.0


def test_self_time_and_exclusive_counters():
    agg, join, scan_a, hash_, scan_b = parse_plan(EXPLAIN)["nodes"]
    assert agg["self_ms"] == 20.0           # 100 - 80
    assert join["self_ms"] == 26.0          # 80 - (50 + 4)
    assert hash_["self_ms"] == 1.0          # 4 - 3
    assert scan_a["self_shared_read_blks"] == 900
    assert join["self_shared_read_blks"] == 0
    assert agg["self_io_read_ms"] == 0.0
    assert scan_a["relation"] == "a" and scan_a["rows_removed_by_filter"] == 100


def test_loops_multiply_total_time():
    plan = {"Plan": {"Node Type": "Index Scan", "Actual Total Time": 0.01, "Actual Loops": 1000}}
    (node,) = parse_plan(plan)["nodes"]
    assert abs(node["total_ms"] - 10.0) < 1e-9
    assert node["shared_read_blks"] == 0    # missing counters default to 0
