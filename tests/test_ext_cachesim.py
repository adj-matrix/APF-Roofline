"""E4 (tpch.ext.cachesim): the turnover rule for write-back chunks, and the paced reader."""
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from tpch.ext.cachesim.model import tail_limited_extents
from tpch.model import cost
from tpch.model.cost import extents_per_file

MiB = 2**20
PARAMS = {"writeback_expire_s": 30.0, "mballoc_window_MiB": 8.0, "start_extents_vs_chunk_MiB": [[1, 4], [8, 1]]}


class Params:
    def value(self, name):
        return PARAMS[name]


# 64 batches: 63 files per side; outer files of 160 MiB written over 200 s (0.8 MiB/s), inner of 32 MiB over 60 s
JOIN = SimpleNamespace(nbatch=64, temp_bytes=63 * (160 + 32) * MiB, outer_temp_bytes=63 * 160 * MiB)


def test_no_turnover_is_the_base_model():
    base = cost.batch_file_extents(JOIN, Params(), 60.0, 200.0)
    with tail_limited_extents(t_tail=1e9):
        assert cost.batch_file_extents(JOIN, Params(), 60.0, 200.0) == base


def test_turnover_shortens_the_chunks():
    base = cost.batch_file_extents(JOIN, Params(), 60.0, 200.0)
    with tail_limited_extents(t_tail=10.0):
        tight = cost.batch_file_extents(JOIN, Params(), 60.0, 200.0)
    # per file: outer 160 MiB at 0.8 MiB/s, inner 32 MiB at 0.533 MiB/s; chunk = rate x min(30 s, t_tail)
    curve, window = PARAMS["start_extents_vs_chunk_MiB"], PARAMS["mballoc_window_MiB"]
    expect = lambda t: 63 * (extents_per_file(32, min(32, 32 / 60 * t), window, curve)
                             + extents_per_file(160, min(160, 0.8 * t), window, curve))
    assert abs(base - expect(30)) < 1e-9
    assert abs(tight - expect(10)) < 1e-9
    assert tight > 2 * base


def test_the_base_function_is_restored():
    original = cost.batch_file_extents
    with tail_limited_extents(t_tail=5.0):
        assert cost.batch_file_extents is not original
    assert cost.batch_file_extents is original
    try:
        with tail_limited_extents(t_tail=5.0):
            raise RuntimeError
    except RuntimeError:
        pass
    assert cost.batch_file_extents is original


def test_reader_paces_and_stops(tmp_path):
    f = tmp_path / "data"
    f.write_bytes(bytes(4 * MiB))
    lst = tmp_path / "list"
    lst.write_text(f"{f}\n")
    t0 = time.monotonic()
    src = str(Path(__file__).resolve().parents[1] / "src")
    out = subprocess.run([sys.executable, "-m", "tpch.ext.cachesim.reader", str(lst), "8", "10"],
                         capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": src}).stdout
    took = time.monotonic() - t0
    assert "read 4 MiB" in out and "end of list" in out
    assert took >= 0.45                                   # 4 MiB at 8 MiB/s: about half a second, not instant
