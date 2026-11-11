import csv

import pytest

from tpch import config
from tpch.bench.location import (BS, CHUNK, COLUMNS, dd_cmd, load_scan, mean_physical, parse_filefrag,
                                 piece_row, pieces, run_location_bench, table_bandwidth)

FILEFRAG = """Filesystem type is: ef53
File size of /srv/tpch/pgdata/base/16384/16632 is 922050560 (225110 blocks of 4096 bytes)
 ext:     logical_offset:        physical_offset: length:   expected: flags:
   0:        0..    2047:   25032704..  25034751:   2048:
   1:     2048..    6143:   25049088..  25053183:   4096:   25034752:
"""


def test_parse_filefrag_bytes():
    ex = parse_filefrag(FILEFRAG)
    assert ex == [(0, 25032704 * 4096, 2048 * 4096), (2048 * 4096, 25049088 * 4096, 4096 * 4096)]


def test_mean_physical_is_byte_weighted():
    ex = [(0, 100, 10), (10, 1000, 30)]               # 10 bytes around 105, 30 bytes around 1015
    assert mean_physical(ex, 0, 40) == pytest.approx((10 * 105 + 30 * 1015) / 40)
    assert mean_physical(ex, 10, 20) == pytest.approx(1005)


def test_pieces_are_aligned_and_cover_the_file():
    size = 2 * CHUNK + 3 * BS + 100                     # trailing partial block skipped
    ps = pieces(size)
    assert [o for o, _ in ps] == [0, CHUNK, 2 * CHUNK]
    assert all(o % BS == 0 and n % BS == 0 for o, n in ps)
    assert sum(n for _, n in ps) == size // BS * BS


def test_reads_are_direct_and_from_files():
    cmd = dd_cmd("/srv/tpch/bench/fio.dat", CHUNK, CHUNK)
    assert "iflag=direct" in cmd and "of=/dev/null" in cmd
    assert f"skip={CHUNK // BS}" in cmd and f"count={CHUNK // BS}" in cmd
    assert not any(a.startswith(("if=/dev/", "of=/srv")) for a in cmd)


def test_cache_hit_flag_from_device_latency():
    disk = piece_row("t", "f", 0, CHUNK, 0, 2.8, {"reads": 4096, "read_ms": 4096 * 0.68})
    cached = piece_row("t", "f", 0, CHUNK, 0, 0.45, {"reads": 4096, "read_ms": 4096 * 0.11})
    assert disk["cache_hit"] == 0 and disk["r_await_ms"] == pytest.approx(0.68)
    assert cached["cache_hit"] == 1


def test_dry_run_evicts_before_any_read(tmp_path):
    cfg = config.load(env={"RESULTS_DIR": str(tmp_path), "DEVICE_CACHE_EVICT_GIB": "2"})
    _, sh = run_location_bench(cfg, [("fio.dat", ["/srv/tpch/bench/fio.dat"])], dry_run=True)
    log = sh.log
    first_read = next(i for i, c in enumerate(log) if c.startswith("dd iflag=direct"))
    evict = next(i for i, c in enumerate(log) if "of=/srv/tpch/bench/evict.tmp" in c)
    assert evict < first_read
    assert any(c.startswith("sleep") for c in log[evict:first_read])


def test_bandwidth_skips_cache_hits_and_reads_old_scans(tmp_path):
    p = tmp_path / "location.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerow({"target": "sf1.lineitem", "file": "/x/base/1/2", "offset_bytes": 0, "bytes": 2**29,
                    "phys_GiB": 1, "seconds": 2.0, "MiBps": 256, "dev_reads": 1, "r_await_ms": 0.7, "cache_hit": 0})
        w.writerow({"target": "sf1.lineitem", "file": "/x/base/1/2.1", "offset_bytes": 0, "bytes": 2**29,
                    "phys_GiB": 2, "seconds": 0.5, "MiBps": 1024, "dev_reads": 1, "r_await_ms": 0.1, "cache_hit": 1})
    rows = load_scan(p)
    assert table_bandwidth(rows, "base/1/2") == pytest.approx(256)
    old = tmp_path / "old.csv"                         # format before eviction existed
    old.write_text("file,offset_bytes,bytes,phys_GiB,seconds,MiBps\n/x/f,0,1048576,3.0,0.01,100\n")
    r = load_scan(old)[0]
    assert r["target"] is None and r["r_await_ms"] is None and not r["cache_hit"]
