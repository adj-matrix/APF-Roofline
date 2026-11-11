import os

import pytest

from tpch import config
from tpch.runner import query_sql, run_query


@pytest.fixture
def cfg():
    return config.load(env={"PATH": os.environ["PATH"]})


def test_cold_run_sequence(cfg):
    runs, sh = run_query(cfg, "q6", 10, cache_state="cold", mode="analyze", repeat=2,
                         settings={"work_mem": "256MB"}, dry_run=True)
    assert len(runs) == 2
    assert not any(r.exists() for r in runs)          # dry run touches nothing
    log = sh.log
    assert len(log) == 2 * 5                          # stop, sync, drop, start, query per repeat
    assert log[0].endswith("-m fast stop") and log[3].endswith(" start")
    assert "drop_caches" in log[2]
    assert "SET work_mem = 256MB" in log[4]
    assert "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT" in log[4]


def test_warm_run_does_not_touch_caches(cfg):
    _, sh = run_query(cfg, "q3", 1, cache_state="warm", dry_run=True)
    assert len(sh.log) == 1 and "<warm-up" in sh.log[0]


def test_query_sql_strips_comments_and_semicolon(cfg):
    sql = query_sql(cfg, "q6")
    assert sql.startswith("SELECT") and not sql.endswith(";") and "--" not in sql


@pytest.mark.parametrize("kw", [{"mode": "fast"}, {"cache_state": "hot"}])
def test_rejects_unknown_options(cfg, kw):
    with pytest.raises(ValueError):
        run_query(cfg, "q6", 1, dry_run=True, **kw)
