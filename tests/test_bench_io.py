import json
import os
from pathlib import Path

import pytest

from tpch import config
from tpch.bench.io import GiB, IoTest, default_sweep, fio_cmd, parse_fio, run_io_bench

FIX = Path(__file__).parent / "fixtures"


def test_every_measurement_is_readonly():
    for t in default_sweep():
        cmd = fio_cmd(t, Path("/srv/tpch/bench/fio.dat"), 0, 4 * GiB, 15)
        assert "--readonly" in cmd
        assert any(a.startswith("--rw=") and a in ("--rw=read", "--rw=randread") for a in cmd)
        assert not any(a.startswith("--filename=/dev/") for a in cmd)


def test_test_names_are_unique_and_readable():
    names = [t.name for t in default_sweep()]
    assert len(names) == len(set(names))
    assert IoTest("seq", 8192, False).name == "seq_buffered_8k_t1"
    assert IoTest("rand", 1 << 20, True).name == "rand_direct_1m_t1"


def test_parse_fio_real_output():
    row = parse_fio(json.loads((FIX / "fio_seq_direct_128k.json").read_text()))
    assert row["runtime_s"] == pytest.approx(1.001)
    assert row["io_bytes"] == 1217134592
    assert row["MiBps"] == pytest.approx(1159.59, rel=1e-4)
    assert row["MiBps"] * (1 << 20) / 131072 == pytest.approx(row["iops"], rel=1e-3)  # bs consistency
    assert row["lat_avg_ms"] == pytest.approx(0.0997, rel=1e-3)
    assert row["lat_p99_ms"] == pytest.approx(0.146432)


def test_dry_run_plan():
    cfg = config.load(env={"PATH": os.environ["PATH"]})
    tests = [IoTest("seq", 8192, True), IoTest("seq", 8192, False)]
    _, sh = run_io_bench(cfg, file_size=16 * GiB, tests=tests, dry_run=True)
    log = sh.log
    assert "--rw=write" in log[0] and "--readonly" not in log[0]      # prepare is the only write
    measurements = [c for c in log if c.startswith("fio --name=seq")]
    assert len(measurements) == 2 and all("--readonly" in c for c in measurements)
    assert "--offset=0" in measurements[0] and f"--offset={4 * GiB}" in measurements[1]
    assert sum("drop_caches" in c for c in log) == 2


def test_offsets_are_aligned_and_rotate():
    cfg = config.load(env={"PATH": os.environ["PATH"]})
    _, sh = run_io_bench(cfg, file_size=16 * GiB, region=4 * GiB, dry_run=True)
    offsets = [int(a.split("=")[1]) for c in sh.log if c.startswith("fio --name=")
               and "--readonly" in c for a in c.split() if a.startswith("--offset=")]
    assert len(offsets) == len(default_sweep())
    assert all(o % (1 << 20) == 0 and o + 4 * GiB <= 16 * GiB for o in offsets)
    assert all(a != b for a, b in zip(offsets, offsets[1:]))
