from __future__ import annotations

from dataclasses import dataclass, field

MiB = 1 << 20
PG_PAGE = 8192


@dataclass(frozen=True)
class TableStats:
    name: str
    pages: int          # pg_class.relpages (8 KiB each)
    tuples: int         # pg_class.reltuples

    @property
    def bytes(self) -> int:
        return self.pages * PG_PAGE


@dataclass(frozen=True)
class HwParams:
    """Every parameter carries where it came from, so a report can cite it."""
    seq_read_MiBps: float           # device sequential read bandwidth at the query's request size
    cpu_ns_per_tuple: float         # executor cost per scanned tuple, data already in memory
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class Prediction:
    total_s: float                       # point estimate (see `combine`)
    lower_s: float                       # IO and CPU fully overlapped: max(io, cpu)
    upper_s: float                       # fully serialized: io + cpu
    components: dict[str, float]         # e.g. {"io_s": .., "cpu_s": ..}
    bound: str                           # "io" | "cpu": the larger component
    assumptions: list[str]

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def combine(components: dict[str, float], assumptions: list[str], how: str = "max") -> Prediction:
    io, cpu = components["io_s"], components["cpu_s"]
    lower, upper = max(io, cpu), io + cpu
    if how not in ("max", "sum"):
        raise ValueError("combine must be 'max' or 'sum'")
    return Prediction(
        total_s=lower if how == "max" else upper, lower_s=lower, upper_s=upper,
        components=components, bound="io" if io >= cpu else "cpu",
        assumptions=assumptions + [f"IO/CPU combined as {how}; true value expected in [max, sum]"],
    )
