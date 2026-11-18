"""Plan trees from EXPLAIN (FORMAT JSON), as the model sees them.

Two kinds of plan feed the model, and they must not be mixed up:
  target plan       EXPLAIN at the scale factor being predicted, *without* ANALYZE (or an ANALYZE plan
                    read through `estimates_only`). Gives the shape, and the planner's row and width
                    estimates, which is what the executor sizes hash tables from.
  calibration plan  EXPLAIN ANALYZE of the same plan shape at a small scale factor. Gives the actual
                    rows of every node; the model scales them to the target.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

# The node types the model has been validated on. Anything else is refused, not guessed.
SUPPORTED = {"Seq Scan", "Hash", "Hash Join", "Aggregate", "Sort", "Limit"}
ACTUAL_KEYS = ("Actual Rows", "Actual Loops", "Actual Total Time", "Temp Written Blocks",
               "Temp Read Blocks", "Shared Read Blocks", "Hash Batches", "Hash Buckets",
               "Peak Memory Usage", "HashAgg Batches", "Disk Usage")


class UnsupportedPlan(ValueError):
    """The plan has a node type or shape the model has not been validated on."""


@dataclass
class Node:
    """One plan node, with the fields the model reads; everything else stays in `raw`."""
    type: str
    relation: str | None
    strategy: str | None            # Aggregate: Plain / Hashed / Sorted
    plan_rows: float
    plan_width: int
    actual_rows: float | None       # rows over all loops; None in an estimate-only plan
    raw: dict                       # the node's own keys (no "Plans")
    children: list["Node"] = field(default_factory=list)

    def walk(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def label(self) -> str:
        return self.type + (f" {self.strategy}" if self.strategy else "") + (f" on {self.relation}" if self.relation else "")


def from_explain(doc: list | dict, estimates_only: bool = False) -> Node:
    """Parse EXPLAIN (FORMAT JSON) output.

    Args:
        doc: the JSON document (a list with one element, or that element).
        estimates_only: drop every measured value (actual rows, buffers, temp blocks, timings), so an
            EXPLAIN ANALYZE plan can be used as a target plan without leaking measurements.

    Returns:
        The root Node.
    """
    top = doc[0] if isinstance(doc, list) else doc
    return _node(top["Plan"], estimates_only)


def _node(plan: dict, estimates_only: bool) -> Node:
    raw = {k: v for k, v in plan.items() if k != "Plans"}
    if estimates_only:
        raw = {k: v for k, v in raw.items()
               if k not in ACTUAL_KEYS and not k.startswith(("Actual", "Shared", "Temp", "Local", "I/O"))}
    actual = None
    if not estimates_only and "Actual Rows" in plan:
        actual = float(plan["Actual Rows"]) * float(plan.get("Actual Loops", 1))
    return Node(type=plan["Node Type"], relation=plan.get("Relation Name"), strategy=plan.get("Strategy"),
                plan_rows=float(plan.get("Plan Rows", 0)), plan_width=int(plan.get("Plan Width", 0)),
                actual_rows=actual, raw=raw,
                children=[_node(child, estimates_only) for child in plan.get("Plans", [])])


def shape(node: Node) -> tuple:
    """What must be equal for a calibration plan to stand in for a target plan: node types,
    relations and aggregate strategies, recursively."""
    return (node.type, node.relation, node.strategy, tuple(shape(child) for child in node.children))


def check_supported(root: Node) -> None:
    """Raise UnsupportedPlan unless every node is of a type in SUPPORTED, in a shape the model knows."""
    unsupported = sorted({x.label for x in root.walk() if x.type not in SUPPORTED})
    if unsupported:
        raise UnsupportedPlan(f"node types the model does not cover: {', '.join(unsupported)}")
    for x in root.walk():
        if x.type == "Hash Join" and (len(x.children) != 2 or x.children[1].type != "Hash"):
            raise UnsupportedPlan("Hash Join without a Hash inner child")


def pair(target: Node, cal: Node) -> list[tuple[Node, Node]]:
    """Pre-order pairs (target node, calibration node); raises if the shapes differ."""
    if shape(target) != shape(cal):
        raise UnsupportedPlan("calibration plan has a different shape than the target plan:\n"
                              f"  target: {shape(target)}\n  cal:    {shape(cal)}")
    return list(zip(target.walk(), cal.walk()))
