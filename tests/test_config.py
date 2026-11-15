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


def test_volume_follows_scale_factor(tmp_path):
    big = tmp_path / "big"
    (big / "pg").mkdir(parents=True)
    link = tmp_path / "pgdata_tblspc"            # like PGDATA/pg_tblspc/<oid> -> BIG_ROOT/pg
    link.symlink_to(big / "pg")
    cfg = config.load(env={"PATH": os.environ["PATH"], "BIG_ROOT": str(big), "DATA_ROOT": "/x"})
    assert (cfg.root(10), cfg.device(10)) == ("/x", "sda")
    assert (cfg.root(100), cfg.device(100)) == (str(big), "sdb")
    assert cfg.volume_of(link / "16384" / "1234") == (str(big), "sdb")   # symlink resolved
    assert cfg.volume_of("/x/pgdata/base/5/1234") == ("/x", "sda")
    assert cfg.volume("big") == (str(big), "sdb")
