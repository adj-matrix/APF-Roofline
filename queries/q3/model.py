"""Q3 model: three Seq Scans in sequence, joined by two in-memory hash joins.

Plan (PostgreSQL 18, SF1 and SF10, no parallelism):

    Limit <- Sort (top-N) <- HashAggregate <- Hash Join (l_orderkey = o_orderkey)
                                                ├─ Seq Scan lineitem  (l_shipdate > d)        probe side
                                                └─ Hash <- Hash Join (o_custkey = c_custkey)  build side
                                                            ├─ Seq Scan orders (o_orderdate < d)
                                                            └─ Hash <- Seq Scan customer (BUILDING)

Execution order = three phases, each one Seq Scan: customer (build inner hash), orders (probe it,
build the outer hash), lineitem (probe the outer hash, aggregate).

Cold: each phase is I/O-bound if its CPU work is hidden behind the scan, as in Q6.
    io_s = sum over tables of bytes / bandwidth at that table's location

Warm: CPU only. Calibrated on SF1 (cycles), scaled by tuple counts, plus the one thing that does
not scale linearly: a probe of a hash table larger than the LLC waits for DRAM.
    probe cost ~ (1 + alpha) dependent cache-line loads (bucket, then each tuple of the chain),
    alpha = tuples / buckets (PostgreSQL: buckets = next power of 2 >= tuples)
    each load costs the latency of a random access in a working set of the hash table's size
    (pointer-chase microbenchmark, ./tpch bench mem)
"""
from __future__ import annotations

from dataclasses import dataclass

HJTUPLE_OVERHEAD = 16           # MAXALIGN(sizeof(HashJoinTupleData)): next pointer + hash value
MINIMAL_TUPLE_HEADER = 16       # MAXALIGN'ed with the data below
BUCKET_BYTES = 8                # one pointer per bucket


def maxalign(n: int) -> int:
    return (n + 7) // 8 * 8


def next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


@dataclass(frozen=True)
class HashTable:
    tuples: int
    data_width: int             # bytes of the stored columns (all int4/date here: 4 each)

    @property
    def buckets(self) -> int:
        return next_pow2(self.tuples)

    @property
    def alpha(self) -> float:   # load factor = mean chain length
        return self.tuples / self.buckets

    @property
    def bucket_bytes(self) -> int:
        return self.buckets * BUCKET_BYTES

    @property
    def tuple_bytes(self) -> int:
        return self.tuples * (HJTUPLE_OVERHEAD + maxalign(MINIMAL_TUPLE_HEADER + self.data_width))

    @property
    def bytes(self) -> int:
        return self.tuple_bytes + self.bucket_bytes

    def batches(self, hash_mem_bytes: int) -> int:
        """1 if it fits in work_mem * hash_mem_multiplier, else the power of 2 that makes it fit."""
        b = 1
        while self.bytes / b > hash_mem_bytes:
            b *= 2
        return b

    def loads_per_probe(self) -> float:
        """Dependent cache-line loads of an unsuccessful probe: bucket + every tuple of its chain."""
        return 1 + self.alpha


def io_seconds(tables: dict[str, tuple[float, float]]) -> dict[str, float]:
    """{table: (MiB, MiB/s at its location)} -> {table: seconds}."""
    return {t: mib / bw for t, (mib, bw) in tables.items()}


def probe_ns(probes: int, ht: HashTable, latency_ns: float) -> float:
    """Time spent waiting for the hash table, assuming probes do not overlap each other."""
    return probes * ht.loads_per_probe() * latency_ns


def probe_ns_split(probes: int, ht: HashTable, latency_ns) -> float:
    """v2 (after the measurement): the bucket array and the tuples are two working sets.
    Each probe loads one bucket (random in bucket_bytes) and alpha tuples (random in tuple_bytes);
    latency_ns(size_bytes) is the microbenchmark curve. The bucket array is 1/5 of the bytes but
    gets 1/(1+alpha) of the loads, so at SF10 it stays mostly in the LLC."""
    return probes * (latency_ns(ht.bucket_bytes) + ht.alpha * latency_ns(ht.tuple_bytes))


def warm_seconds_scaled(cycles_small: float, ghz_small: float, probe_ns_small: float,
                        scale: float, ghz_large: float, probe_ns_large: float) -> float:
    """Warm time at a larger SF from a measured smaller SF.

    Everything except waiting for the hash table scales with the tuple count and runs at the
    clock; the hash-table wait is in ns (DRAM latency does not speed up with the core clock)."""
    compute_cycles = cycles_small - probe_ns_small * ghz_small
    return scale * compute_cycles / (ghz_large * 1e9) + probe_ns_large * 1e-9
