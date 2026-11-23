"""E2 (tpch.ext.parallel): the parallel-scan replicas on small files, and the analysis helpers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tpch.ext.parallel.bench import perf_totals
from tpch.ext.parallel.m6 import REGION, windows

SRC = Path(__file__).resolve().parents[1] / "src" / "tpch" / "ext" / "parallel"
needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")


def build(tmp_path, name, *flags):
    out = tmp_path / name
    subprocess.run(["cc", "-O2", "-Wall", *flags, "-o", str(out), str(SRC / f"{name}.c")], check=True)
    return out


def segments(tmp_path, sizes_mib):
    """Small stand-ins for a table's segment files, listed in order."""
    paths = []
    for i, mib in enumerate(sizes_mib):
        p = tmp_path / f"seg.{i}"
        p.write_bytes(bytes(int(mib * 2**20)))
        paths.append(str(p))
    lst = tmp_path / "list"
    lst.write_text("".join(f"{p}\n" for p in paths))
    return lst


@needs_cc
@pytest.mark.parametrize("engine", ["psync", "uring"])
@pytest.mark.parametrize("participants", [1, 3])
def test_replica_par_reads_every_block_once(tmp_path, engine, participants):
    # PostgreSQL's allocation (table_block_parallelscan_nextpage): chunks from one shared counter, ramp-down at
    # the end. Every block of the table is handed out exactly once, so the participants' blocks add up to it.
    binary = build(tmp_path, "replica_par")
    lst = segments(tmp_path, [3, 3, 1.5])                       # 960 blocks of 8 KiB over three files
    r = subprocess.run([str(binary), f"@{lst}", str(participants), engine, "0", "16", "0", "30"],
                       capture_output=True, text=True, check=True)
    out = json.loads(r.stdout)
    assert out["nblocks"] == 960
    assert out["blocks"] == 960 and sum(out["part_blocks"]) == 960
    assert len(out["part_blocks"]) == participants
    assert out["reads"] <= 960 and out["reads"] >= 960 / 16    # reads combine up to 16 consecutive blocks


@needs_cc
def test_replica_par_none_only_works(tmp_path):
    binary = build(tmp_path, "replica_par")
    out = json.loads(subprocess.run([str(binary), "x", "2", "none", "0", "1", "1", "0.2"],
                                    capture_output=True, text=True, check=True).stdout)
    assert out["blocks"] == 0 and out["reads"] > 0 and len(out["part_end_s"]) == 2


@needs_cc
def test_leapq_shares_the_reads_and_checks_batch(tmp_path):
    binary = build(tmp_path, "leapq", "-pthread")
    lst = segments(tmp_path, [8])
    # chunks of 1 MiB from offset 0, phase 0, 3 readers, batch 4: two participants leapfrog over 8 MiB
    out = json.loads(subprocess.run([str(binary), f"@{lst}", "0", "0", "3", "5", "1", "0", "1", "4"],
                                    capture_output=True, text=True, check=True).stdout)
    assert out["batch"] == 4 and out["readers"] == 3
    assert sum(out["reader_reads"]) * 128 * 1024 / 2**20 <= 8 + 2 * 16 * 0.125   # at most the file plus in-flight
    bad = subprocess.run([str(binary), f"@{lst}", "0", "0", "3", "1", "1", "0", "1", "5"], capture_output=True)
    assert bad.returncode == 2                                                    # 16 slots are not a multiple of 5


@needs_cc
def test_twostream_leapfrog_mode(tmp_path):
    binary = build(tmp_path, "twostream")
    lst = segments(tmp_path, [8])
    out = json.loads(subprocess.run([str(binary), f"@{lst}", "0", "0", "5", "0", "1"],
                                    capture_output=True, text=True, check=True).stdout)
    assert out["chunk_MiB"] == 1
    assert sum(out["stream_MiBps"]) > 0


def test_perf_totals_reads_csv(tmp_path):
    f = tmp_path / "perf.csv"
    f.write_text("# started\n\n1000.5,msec,task-clock,1,100.00\n3400000000,,cycles,1,100.00\n"
                 "<not counted>,,instructions,0,0.00\n")
    assert perf_totals(f) == {"task-clock": 1000.5, "cycles": 3400000000.0}


def test_m6_windows_sequential_and_region_switches():
    # four requests of 256 sectors: two continue the previous one, one jumps to another 64 MiB region
    rows = [(0.0, 1, 0, 256), (0.1, 1, 256, 256), (0.2, 2, REGION * 3, 256), (0.3, 2, REGION * 3 + 256, 256)]
    (w,) = windows(rows, 1.0)
    assert w["n"] == 4
    assert w["seq"] == 2 / 4 and w["switch"] == 1 / 4
    assert w["tasks"] == 2 and w["top_task"] == 0.5
    assert abs(w["MBps"] - 4 * 256 * 512 / 1e6) < 1e-9
