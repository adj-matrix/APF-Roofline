from collections import Counter

from tpch import config
from tpch.experiment import conditions, run_experiment, schedule

CONDS = conditions([1, 10], ["cold", "warm"], ["plain"])


def test_conditions_cross_product():
    assert len(CONDS) == 4
    assert {"sf": 10, "cache": "warm", "mode": "plain"} in CONDS


def test_every_block_has_every_condition_once():
    order = schedule(CONDS, blocks=5, seed=7)
    assert len(order) == 20
    for r in range(5):
        block = [(o["sf"], o["cache"]) for o in order if o["block"] == r]
        assert Counter(block) == Counter((c["sf"], c["cache"]) for c in CONDS)


def test_seed_is_deterministic_and_blocks_differ():
    a, b = schedule(CONDS, 5, seed=7), schedule(CONDS, 5, seed=7)
    assert a == b
    orders = {tuple((o["sf"], o["cache"]) for o in a if o["block"] == r) for r in range(5)}
    assert len(orders) > 1                    # not the same order every block
    assert schedule(CONDS, 5, seed=8) != a


def test_dry_run_experiment_order_and_prev(tmp_path):
    cfg = config.load(env={"RESULTS_DIR": str(tmp_path)})
    path, sh = run_experiment(cfg, "q6", CONDS, blocks=2, seed=3, label="t", dry_run=True)
    order = schedule(CONDS, 2, seed=3)
    psql = [ln for ln in sh.log if ln.startswith("[psql")]
    assert len(psql) == 4 * 2
    for line, o in zip(psql, order):
        assert line.startswith(f"[psql tpch_sf{o['sf']}]")
        assert ("<warm-up" in line) == (o["cache"] == "warm")
    colds = sum(o["cache"] == "cold" for o in order)
    assert sum("drop_caches" in ln for ln in sh.log) == colds
    assert not path.exists()                  # dry run writes nothing


def test_cold_experiment_evicts_device_cache_once_first(tmp_path):
    cfg = config.load(env={"RESULTS_DIR": str(tmp_path), "DEVICE_CACHE_EVICT_GIB": "2"})
    _, sh = run_experiment(cfg, "q6", CONDS, blocks=2, seed=3, dry_run=True)
    dd = [i for i, ln in enumerate(sh.log) if ln.startswith("dd ") and "evict.tmp" in ln]
    assert len(dd) == 1
    first_psql = next(i for i, ln in enumerate(sh.log) if ln.startswith("[psql"))
    first_stop = next(i for i, ln in enumerate(sh.log) if "pg_ctl" in ln)
    assert dd[0] < min(first_psql, first_stop)
    assert "count=2048" in sh.log[dd[0]] and "oflag=direct" in sh.log[dd[0]]
    assert sh.log[dd[0]].split()[2].startswith(f"of={cfg.DATA_ROOT}/bench/")   # a file, never a device


def test_warm_only_experiment_does_not_evict(tmp_path):
    cfg = config.load(env={"RESULTS_DIR": str(tmp_path)})
    _, sh = run_experiment(cfg, "q6", conditions([1], ["warm"], ["plain"]), blocks=1, seed=0, dry_run=True)
    assert not any("evict.tmp" in ln for ln in sh.log)


def test_several_queries_are_a_factor(tmp_path):
    conds = conditions([1], ["warm"], ["plain"], ["q1.m_scan", "q1.m_filter"])
    assert conds == [{"query": "q1.m_scan", "sf": 1, "cache": "warm", "mode": "plain"},
                     {"query": "q1.m_filter", "sf": 1, "cache": "warm", "mode": "plain"}]
    assert conditions([1], ["warm"], ["plain"], ["q6"]) == conditions([1], ["warm"], ["plain"])
    cfg = config.load(env={"RESULTS_DIR": str(tmp_path)})
    path, sh = run_experiment(cfg, "q1.m_scan", conds, blocks=1, seed=1, label="t", dry_run=True)
    assert path.name.endswith("_q1_t.json")
    psql = [ln for ln in sh.log if ln.startswith("[psql")]
    assert any("count(*)" in ln and "l_shipdate" in ln for ln in psql)   # m_filter's text
