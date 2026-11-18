"""What a query has to do at a target scale factor: tuples, pages, batches, spilled tuples, temp bytes.

Machine-independent. Built from
  * the target plan (shape + planner estimates; the executor sizes hash tables from these),
  * the calibration plan (EXPLAIN ANALYZE of the same shape at a small SF): actual rows, scaled by
    SF_target / SF_cal (every TPC-H table but nation and region is linear in SF),
  * the target database's catalog (pages and tuples of each table),
  * the settings (work_mem, hash_mem_multiplier).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Config
from . import pg
from .plan import Node, check_supported, pair

MiB = 1 << 20


@dataclass
class Scan:
    """A sequential scan of a table at the target SF (pages and tuples from the target's catalog)."""
    relation: str
    pages: int
    tuples: float

    @property
    def mib(self) -> float:
        return self.pages * pg.BLCKSZ / MiB


@dataclass
class HashJoinSpill:
    """A hash join at the target SF, sized the way PostgreSQL sizes it (pg.choose_hash_table_size).

    Rows are the calibration plan's actual rows x scale; widths are the target planner's estimates.
    With nbatch = 1 nothing spills and the byte and tuple counts are 0.
    """
    label: str                      # "hash on <relations of the inner side>"
    nbatch: int
    nbuckets: int
    inner_rows: float
    outer_rows: float
    inner_width: int                # planner's width (bytes of data per tuple)
    outer_width: int
    spilled_tuples: float           # inner + outer tuples written to batch files (and read back)
    temp_bytes: float
    outer_temp_bytes: float         # written while the outer side is scanned (page-cache model)
    outer_scan_mib: float = 0.0     # tables read by the outer side while its batches are written
    false_hash_matches: float = 0.0  # probes that find an inner tuple with their hash but another key

    @property
    def batch_inner_mib(self) -> float:
        """Inner data per batch (planner widths): the size of the hash table each batch is joined in."""
        return self.inner_rows * self.inner_width / self.nbatch / MiB


@dataclass
class AggSpill:
    """A hashed aggregate that spills: approximate (see DEFAULT_AGG_ENTRY)."""
    groups: float
    input_rows: float
    entry_bytes: float
    spilled_tuples: float
    temp_bytes: float


@dataclass
class Workload:
    """Everything the cost model needs to know about a query at a target SF; no machine parameter."""
    query: str
    sf: int
    sf_cal: int
    settings: dict
    scale: float                                   # rows at target / rows in the calibration plan
    bigint_keys: bool = False                      # o_orderkey / l_orderkey are BIGINT (bigint_keys())
    scans: list[Scan] = field(default_factory=list)
    joins: list[HashJoinSpill] = field(default_factory=list)
    aggs: list[AggSpill] = field(default_factory=list)

    @property
    def table_pages(self) -> int:
        return sum(s.pages for s in self.scans)

    @property
    def table_mib(self) -> float:
        return sum(s.mib for s in self.scans)

    @property
    def spilled_tuples(self) -> float:
        return sum(j.spilled_tuples for j in self.joins) + sum(a.spilled_tuples for a in self.aggs)

    @property
    def temp_bytes(self) -> float:
        return sum(j.temp_bytes for j in self.joins) + sum(a.temp_bytes for a in self.aggs)

    @property
    def temp_pages(self) -> float:
        return self.temp_bytes / pg.BLCKSZ

    @property
    def false_hash_matches(self) -> float:
        return sum(j.false_hash_matches for j in self.joins)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(table_pages=self.table_pages, table_mib=self.table_mib, spilled_tuples=self.spilled_tuples,
                 temp_bytes=self.temp_bytes, false_hash_matches=self.false_hash_matches)
        return d


def bigint_keys(sf: int) -> bool:
    """Whether tpch_sf<N> has BIGINT order keys: the largest order key, 6,000,000 x SF, exceeds INTEGER
    (src/setup/setup.sh, bigkeys(); SF >= 358)."""
    return sf * 6_000_000 > 2**31 - 1


ORDER_KEYS_PER_SF = 6_000_000     # the largest order key is 6,000,000 x SF (dbgen: sparse keys, 8 of every 32)


def twin_key_fraction(max_key: float) -> float:
    """Share of the order keys (uniform over 1..max_key) whose 32-bit hash another order key shares.

    PostgreSQL hashes a BIGINT as hash_uint32(lo32 xor hi32) (hashint8): a key a >= 2^32 (below 2^33) has
    the twin (a - 2^32) xor 1, another valid sparse order key with the same lo32 xor hi32, hence the same
    hash. Keys in [2^32, max_key] and their twins in [1, max_key - 2^32] are both twinned. (Keys below 2^32
    collide only by chance, like any 32-bit hash: false_hash_matches.)

    Raises:
        NotImplementedError: max_key >= 2^33 (SF > 1431: three or more keys per hash value).
    """
    if max_key <= 2**32:
        return 0.0
    if max_key >= 2**33:
        raise NotImplementedError("order keys >= 2^33: more than two keys per 32-bit hash")
    return 2 * (max_key - 2**32) / max_key


def false_hash_matches(hash_cond: str, inner_relations: list[str], outer_rows: float, inner_rows: float,
                       catalog: dict, twin_fraction: float) -> float:
    """Probes of a hash join that meet an inner tuple with their 32-bit hash but another key (and compare it).

    Two sources, both growing with SF^2 in total:
      * chance: the hash is 32 bits, so a probe shares it with each other inner key with probability 2^-32:
        inner_rows / 2^32 per probe (SF100 Q3: 1.7 %, SF1000: 17 %; measured on tpch_sf10_twinkeys: the
        pairs beyond the twins match 15e6^2 / 2^32);
      * twins (order-key joins whose hash table is built on orders, Q3): a probe's key has a twin with
        probability twin_fraction, and the twin's order is in the hash table with probability
        inner_rows / orders.
    """
    per_probe = inner_rows / 2**32
    if twin_fraction and "o_orderkey" in hash_cond and inner_relations == ["orders"]:
        per_probe += twin_fraction * inner_rows / catalog["orders"]["tuples"]
    return outer_rows * per_probe


DEFAULT_AGG_ENTRY = 400.0   # bytes per in-memory group (assumption; PG sizes it from the planner's group estimate)


def build(query: str, target: Node, cal: Node, sf: int, sf_cal: int, catalog: dict, settings: dict,
          temp_bytes_ratio: float = 1.0, twin_fraction: float | None = None) -> Workload:
    """The workload of a query at a target scale factor.

    Args:
        query: query name, e.g. "q3" (selects the per-query parameters later).
        target: the plan at the target SF; only its shape and the planner's estimates are used.
        cal: EXPLAIN ANALYZE of the same shape at sf_cal; its actual rows are scaled by sf / sf_cal.
        sf: target scale factor.
        sf_cal: scale factor of the calibration plan.
        catalog: {relation: {"pages", "tuples"}} of the target database (catalog()).
        settings: effective settings; work_mem and hash_mem_multiplier are used.
        temp_bytes_ratio: measured / formula bytes per spilled hash-join tuple (from calibration).
        twin_fraction: share of order keys with a hash twin; default twin_key_fraction of tpch_sf<sf>'s
            largest key (a key-space variant such as tpch_sf10_twinkeys passes its own).

    Returns:
        The Workload.

    Raises:
        UnsupportedPlan: the target has a node type the model does not cover, or the shapes differ.
        ValueError: the calibration plan has no actual rows.
    """
    check_supported(target)
    scale = sf / sf_cal
    work_mem = pg.parse_mem(settings.get("work_mem", "4MB"))
    hash_mem_multiplier = float(settings.get("hash_mem_multiplier", 2.0))
    if twin_fraction is None:
        twin_fraction = twin_key_fraction(sf * ORDER_KEYS_PER_SF)
    work = Workload(query=query, sf=sf, sf_cal=sf_cal, settings=dict(settings), scale=scale,
                    bigint_keys=bigint_keys(sf) and not bigint_keys(sf_cal))
    for node, cal_node in pair(target, cal):
        if cal_node.actual_rows is None:
            raise ValueError("calibration plan has no actual rows (needs EXPLAIN ANALYZE)")
        if node.type == "Seq Scan":
            table = catalog[node.relation]
            work.scans.append(Scan(node.relation, table["pages"], table["tuples"]))
        elif node.type == "Hash Join":
            outer, inner = node.children                      # inner is the Hash node
            cal_outer, cal_inner = cal_node.children
            sizing = pg.choose_hash_table_size(inner.plan_rows, inner.plan_width, work_mem, hash_mem_multiplier)
            spilled_share = pg.spill_fraction(sizing.nbatch)
            inner_rows, outer_rows = cal_inner.actual_rows * scale, cal_outer.actual_rows * scale
            inner_bytes = spilled_share * inner_rows * pg.spilled_tuple_bytes(inner.plan_width) * temp_bytes_ratio
            outer_bytes = spilled_share * outer_rows * pg.spilled_tuple_bytes(outer.plan_width) * temp_bytes_ratio
            inner_relations = sorted({x.relation for x in inner.walk() if x.relation})
            outer_scan_pages = sum(catalog[x.relation]["pages"] for x in outer.walk() if x.type == "Seq Scan")
            work.joins.append(HashJoinSpill(
                label="hash on " + "+".join(inner_relations), nbatch=sizing.nbatch, nbuckets=sizing.nbuckets,
                inner_rows=inner_rows, outer_rows=outer_rows, inner_width=inner.plan_width,
                outer_width=outer.plan_width, spilled_tuples=spilled_share * (inner_rows + outer_rows),
                temp_bytes=inner_bytes + outer_bytes, outer_temp_bytes=outer_bytes,
                outer_scan_mib=outer_scan_pages * pg.BLCKSZ / MiB,
                false_hash_matches=false_hash_matches(node.raw.get("Hash Cond", ""), inner_relations, outer_rows,
                                                      inner_rows, catalog, twin_fraction)))
        elif node.type == "Aggregate" and node.strategy == "Hashed":
            groups, input_rows = cal_node.actual_rows * scale, cal_node.children[0].actual_rows * scale
            # Approximate (a known gap): PostgreSQL's HashAgg spill depends on the planner's group estimate
            # and re-spills partitions; Q3 x SF100 64MB measures ~5x this volume, still < 5 % of the temp.
            needed = groups * DEFAULT_AGG_ENTRY
            spilled = input_rows * max(0.0, 1 - work_mem * hash_mem_multiplier / needed)
            work.aggs.append(AggSpill(groups=groups, input_rows=input_rows, entry_bytes=DEFAULT_AGG_ENTRY,
                                      spilled_tuples=spilled,
                                      temp_bytes=spilled * pg.spilled_tuple_bytes(node.children[0].plan_width)))
    return work


def catalog(cfg: Config, sf: int, refresh: bool = False) -> dict:
    """Pages and tuples of every table of database tpch_sf<N> (pg_class), cached.

    Args:
        cfg: configuration (database connection).
        sf: scale factor.
        refresh: query the database even if model/catalog/sf<N>.json exists.

    Returns:
        {relation: {"pages": int, "tuples": float}}.
    """
    path = cfg.repo / "model" / "catalog" / f"sf{sf}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    import psycopg
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname=cfg.dbname(sf)) as conn:
        rows = conn.execute(
            "SELECT relname, relpages, reltuples::float8 FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'").fetchall()
    tables = {name: {"pages": pages, "tuples": tuples} for name, pages, tuples in rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tables, indent=1, sort_keys=True) + "\n")
    return tables


def explain(cfg: Config, query: str, sf: int, settings: dict, refresh: bool = False) -> list:
    """EXPLAIN (FORMAT JSON) of a query, without executing it; cached in model/plans/.

    The cached plan is part of a prediction's inputs (commit it with the prediction).

    Args:
        cfg: configuration (database connection).
        query: query name under queries/ (e.g. "q3").
        sf: scale factor (database tpch_sf<N>).
        settings: settings applied before EXPLAIN (they are part of the cache key).
        refresh: EXPLAIN again even if cached.

    Returns:
        The EXPLAIN JSON document (plan.from_explain reads it).
    """
    from ..runner import query_sql
    tag = "_".join(f"{k}={v}" for k, v in sorted(settings.items()))
    path = cfg.repo / "model" / "plans" / f"{query}_sf{sf}{'_' + tag if tag else ''}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    import psycopg
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname=cfg.dbname(sf), autocommit=True) as conn:
        for name, value in settings.items():
            conn.execute("SELECT set_config(%s, %s, false)", (name, value))
        doc = conn.execute("EXPLAIN (FORMAT JSON) " + query_sql(cfg, query)).fetchone()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n")
    return doc


def save(work: Workload, path: Path) -> None:
    path.write_text(json.dumps(work.as_dict(), indent=1) + "\n")
