"""Flatten PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output into a node table."""
from __future__ import annotations

# Plan keys copied to every node row (missing keys -> 0 / None).
_NUMERIC = {
    "Actual Startup Time": "actual_startup_ms",
    "Actual Total Time": "actual_total_ms",
    "Actual Rows": "actual_rows",
    "Actual Loops": "loops",
    "Plan Rows": "plan_rows",
    "Total Cost": "total_cost",
    "Rows Removed by Filter": "rows_removed_by_filter",
    "Shared Hit Blocks": "shared_hit_blks",
    "Shared Read Blocks": "shared_read_blks",
    "Temp Read Blocks": "temp_read_blks",
    "Temp Written Blocks": "temp_written_blks",
    "Shared I/O Read Time": "io_read_ms",
    "Temp I/O Read Time": "temp_io_read_ms",
    "Temp I/O Write Time": "temp_io_write_ms",
}
_TEXT = {
    "Node Type": "type",
    "Relation Name": "relation",
    "Join Type": "join_type",
    "Strategy": "strategy",
    "Sort Method": "sort_method",
    "Filter": "filter",
    "Hash Cond": "hash_cond",
}
# Counters that PostgreSQL reports cumulatively (a node includes its children).
_INCLUSIVE = ["shared_hit_blks", "shared_read_blks", "temp_read_blks", "temp_written_blks", "io_read_ms"]


def parse_plan(explain: list | dict) -> dict:
    """Return {"planning_ms", "execution_ms", "nodes": [...]} from EXPLAIN JSON.

    Each node has `id` (pre-order), `parent`, `depth`, the fields above, and:
      total_ms    actual_total_ms * loops (inclusive wall time of the node)
      self_ms     total_ms minus the children's total_ms
      self_*      exclusive version of each inclusive counter in _INCLUSIVE
    Exclusive values assume a serial plan (no parallel workers).
    """
    top = explain[0] if isinstance(explain, list) else explain
    nodes: list[dict] = []

    def walk(plan: dict, parent: int | None, depth: int) -> int:
        nid = len(nodes)
        row = {"id": nid, "parent": parent, "depth": depth}
        row.update({dst: plan.get(src, 0) or 0 for src, dst in _NUMERIC.items()})
        row.update({dst: plan.get(src) for src, dst in _TEXT.items()})
        row["total_ms"] = row["actual_total_ms"] * row["loops"]
        nodes.append(row)
        children = [walk(child, nid, depth + 1) for child in plan.get("Plans", [])]
        row["self_ms"] = row["total_ms"] - sum(nodes[c]["total_ms"] for c in children)
        for k in _INCLUSIVE:
            row[f"self_{k}"] = row[k] - sum(nodes[c][k] for c in children)
        return nid

    walk(top["Plan"], None, 0)
    return {
        "planning_ms": top.get("Planning Time"),
        "execution_ms": top.get("Execution Time"),
        "nodes": nodes,
    }
