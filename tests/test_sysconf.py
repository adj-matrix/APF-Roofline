"""sysconf: settings are applied at the start and restored at the end, also after a failure or a kill."""
import json

import pytest

from tpch import config, sysconf
from tpch.cache import pg_ctl
from tpch.shell import Shell

ORIG = {"nmi_watchdog": "1", "timers_active": ["plocate-updatedb.timer", "man-db.timer"], "pg_running": True}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = config.load()
    monkeypatch.setattr(sysconf, "state", lambda cfg=None: dict(ORIG))
    return config.Config({**c.values, "RESULTS_DIR": str(tmp_path), "SYSCONF": "watchdog timers pgnuma",
                          "PG_NUMA_NODE": "0"})


def _run(cfg, body=lambda: None, dry_run=True):
    sh = Shell(dry_run)
    with sysconf.applied(cfg, sh):
        sh.log.append("BODY")
        body()
    return sh.log


def test_apply_then_restore_in_reverse_state(cfg):
    log = _run(cfg)
    i = log.index("BODY")
    before, after = " ".join(log[:i]), " ".join(log[i:])
    assert "nmi_watchdog=0" in before and "systemctl stop plocate-updatedb.timer man-db.timer" in before
    assert "numactl --cpunodebind=0 --membind=0" in before and "restart" in before
    assert "nmi_watchdog=1" in after and "systemctl start plocate-updatedb.timer man-db.timer" in after
    assert "restart" in after and "numactl" not in after       # PostgreSQL ends unbound


def test_restore_runs_when_the_body_fails(cfg):
    sh = Shell(True)
    with pytest.raises(RuntimeError):
        with sysconf.applied(cfg, sh):
            raise RuntimeError("query failed")
    assert any("nmi_watchdog=1" in c for c in sh.log)


def test_empty_sysconf_touches_nothing(cfg):
    cfg = config.Config({**cfg.values, "SYSCONF": ""})
    assert _run(cfg) == ["BODY"]
    assert sysconf.pg_numa_node(cfg) is None


def test_killed_command_leaves_originals_for_restore(cfg, monkeypatch):
    # A command killed after applying leaves active.json with the TRUE originals.
    path = sysconf._active_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"knobs": ["watchdog"], "orig": {"nmi_watchdog": "1", "timers_active": []}}))
    # The next command must not take the (already changed) current state as the originals.
    monkeypatch.setattr(sysconf, "state", lambda cfg=None: {"nmi_watchdog": "0", "timers_active": [],
                                                             "pg_running": False})
    sh = Shell(True)
    assert sysconf.restore_interrupted(cfg, sh)["orig"]["nmi_watchdog"] == "1"
    assert sh.log == ["sudo -n sysctl -q -w kernel.nmi_watchdog=1"] or sh.log[-1].endswith("nmi_watchdog=1")


def test_unknown_knob_is_rejected(cfg):
    with pytest.raises(ValueError):
        sysconf.knobs(config.Config({**cfg.values, "SYSCONF": "turbo"}))


def test_pg_ctl_binding(cfg):
    sh = Shell(True)
    pg_ctl(cfg, sh, "start")
    pg_ctl(cfg, sh, "start", bind=False)
    assert sh.log[0].startswith("numactl --cpunodebind=0 --membind=0 ")
    assert "numactl" not in sh.log[1]
