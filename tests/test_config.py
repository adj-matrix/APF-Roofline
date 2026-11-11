import os

from tpch import config


def test_defaults_match_config_env():
    cfg = config.load(env={"PATH": os.environ["PATH"]})
    assert cfg.PG_MAJOR == "18"
    assert cfg.PGDATA == f"{cfg.DATA_ROOT}/pgdata"     # derived values are expanded
    assert cfg.PGBIN == "/usr/lib/postgresql/18/bin"
    assert cfg.sfs == [1, 10]
    assert cfg.dbname(10) == "tpch_sf10"


def test_environment_overrides_like_bash():
    cfg = config.load(env={"PATH": os.environ["PATH"], "TPCH_SFS": "1", "DATA_ROOT": "/x"})
    assert cfg.sfs == [1]
    assert cfg.path("TBL_DIR").as_posix() == "/x/tbl"
