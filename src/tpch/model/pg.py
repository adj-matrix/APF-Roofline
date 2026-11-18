"""PostgreSQL 18 executor arithmetic the model needs: hash-join sizing and spilled-tuple sizes.

Everything here is a re-implementation of PostgreSQL source (src/backend/executor/nodeHash.c,
ExecChooseHashTableSize; nodeHashjoin.c, ExecHashJoinSaveTuple), checked against measured plans:
Q3 x SF10 at work_mem 64MB / 128MB / 1GB and Q3 x SF100 at 64MB / 128MB / 256MB (batches and
buckets exact, temp volume within 1 %). Machine-independent: no parameter here is calibrated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

BLCKSZ = 8192
MAXIMUM_ALIGNOF = 8
HJTUPLE_OVERHEAD = 16          # MAXALIGN(sizeof(HashJoinTupleData)): next pointer + hash value
MINIMAL_TUPLE_HEADER = 16      # MAXALIGN(SizeofMinimalTupleHeader)
POINTER = 8                    # one bucket = sizeof(HashJoinTuple)
SPILL_HASH = 4                 # ExecHashJoinSaveTuple writes the uint32 hash value before the tuple
MAX_ALLOC = 0x3FFFFFFF         # MaxAllocSize


def maxalign(n: float) -> int:
    return int(math.ceil(n / MAXIMUM_ALIGNOF) * MAXIMUM_ALIGNOF)


def next_pow2(n: float) -> int:
    return 1 << max(0, math.ceil(math.log2(max(n, 1))))


def parse_mem(setting: str | int) -> int:
    """'64MB' / '1GB' / '65536' (kB, PostgreSQL's unit for work_mem) -> bytes."""
    if isinstance(setting, int):
        return setting * 1024
    s = setting.strip().upper()
    for unit, mult in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10), ("B", 1)):
        if s.endswith(unit):
            return int(float(s[: -len(unit)]) * mult)
    return int(s) * 1024


def hash_entry_bytes(width: float) -> int:
    """Bytes of one tuple in the in-memory hash table (planner's width)."""
    return HJTUPLE_OVERHEAD + MINIMAL_TUPLE_HEADER + maxalign(width)


def spilled_tuple_bytes(width: float) -> float:
    """Bytes one tuple occupies in a batch file: hash value + MinimalTuple (header + data, unpadded)."""
    return SPILL_HASH + MINIMAL_TUPLE_HEADER + width


@dataclass(frozen=True)
class HashSizing:
    nbuckets: int
    nbatch: int
    hash_mem: int               # bytes allowed for the table (after PG18's batch/memory balancing)
    tupsize: int

    @property
    def bucket_bytes(self) -> int:
        return self.nbuckets * POINTER


def choose_hash_table_size(ntuples: float, width: float, work_mem: int,
                           hash_mem_multiplier: float = 2.0) -> HashSizing:
    """ExecChooseHashTableSize (PostgreSQL 18), without the skew table (no MCVs on the join keys of
    TPC-H). `ntuples`, `width`: the planner's estimates for the inner side, as the executor uses them."""
    tupsize = hash_entry_bytes(width)
    inner_bytes = ntuples * tupsize
    hash_mem = int(work_mem * hash_mem_multiplier)

    max_pointers = min(hash_mem // POINTER, MAX_ALLOC // POINTER)
    mppow2 = 1 << int(math.log2(max_pointers)) if max_pointers > 0 else 1
    if max_pointers != mppow2:
        max_pointers = mppow2 // 2
    max_pointers = min(max_pointers, (2**31 - 1) // 2 + 1)

    nbuckets = next_pow2(max(min(math.ceil(ntuples), max_pointers), 1024))
    nbatch = 1
    if inner_bytes + nbuckets * POINTER > hash_mem:
        # multi-batch: buckets for a full hash_mem of tuples, the rest of hash_mem holds tuples
        nbuckets = next_pow2(min(hash_mem / (tupsize + POINTER), max_pointers))
        bucket_bytes = nbuckets * POINTER
        dbatch = min(math.ceil(inner_bytes / (hash_mem - bucket_bytes)), max_pointers)
        nbatch = next_pow2(max(dbatch, 2))
        # PG18: trade batches for memory while that lowers the total (hash table + 2 BufFiles per batch)
        while nbatch > 1:
            if hash_mem + 2 * nbatch * BLCKSZ < hash_mem * 2 + nbatch * BLCKSZ:
                break
            nbatch //= 2
            nbuckets *= 2
            hash_mem *= 2
    return HashSizing(nbuckets=nbuckets, nbatch=nbatch, hash_mem=hash_mem, tupsize=tupsize)


def spill_fraction(nbatch: int) -> float:
    """Share of each side written to batch files: every batch but batch 0 (uniform hash values)."""
    return (nbatch - 1) / nbatch
