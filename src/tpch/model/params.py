"""Model parameters with provenance.

Every number the cost model uses is a Param. `kind` says how much it can be trusted as a predictor:
  bench       measured by a microbenchmark that runs no TPC-H query (hardware/, ./tpch bench)
  train       estimated from TPC-H runs registered as training data (registry.py), at small SF or,
              where marked, SF100 runs that are excluded from every test
  assumption  set by reasoning, not measured; listed so that it can be challenged
`sources` names the runs / benchmark directories it came from.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

KINDS = ("bench", "train", "assumption")


@dataclass
class Param:
    """One model parameter.

    Attributes:
        name: e.g. "kernel_cycles_per_device_page"; per-query parameters are prefixed "q3." etc.
        value: a number, or a curve [[x, y], ...] read with `interp`.
        unit: unit of the value (of y, for a curve, with "x -> y" naming both).
        kind: "bench", "train" or "assumption" (see the module docstring).
        sources: datasets (registry.py) or benchmark directories it was estimated from.
        note: how it was estimated, and anything a reader should check.
    """
    name: str
    value: float | list            # a number, or a curve: [[x, y], ...]
    unit: str
    kind: str
    sources: list[str] = field(default_factory=list)
    note: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: kind must be one of {KINDS}")


class ParamSet(dict):
    """All parameters of a model version, name -> Param."""

    def add(self, param: Param) -> Param:
        self[param.name] = param
        return param

    def value(self, name: str) -> float | list:
        """The value of parameter `name` (KeyError if it was not calibrated)."""
        return self[name].value

    def digest(self) -> str:
        """12 hex digits identifying the values (not the notes); predictions cite it."""
        blob = json.dumps({k: self[k].value for k in sorted(self)}, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def to_json(self) -> str:
        return json.dumps({"digest": self.digest(), "params": [asdict(self[k]) for k in sorted(self)]}, indent=1)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n")

    @classmethod
    def load(cls, path: Path) -> "ParamSet":
        params = cls()
        for doc in json.loads(path.read_text())["params"]:
            params.add(Param(**doc))
        return params


def interp(curve: list, x: float, log_x: bool = True) -> float:
    """Read a measured curve at x.

    Args:
        curve: points [[x, y], ...], in any order.
        x: where to read it.
        log_x: interpolate linearly in log(x) (for batch counts, CPU time per block) rather than in x.

    Returns:
        y, piecewise linear between the points and clamped at the end points (no extrapolation).
    """
    import math
    pts = sorted(curve)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if log_x:
                x, x0, x1 = math.log(x), math.log(x0), math.log(x1)
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    raise AssertionError
