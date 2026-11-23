"""tpch.procmon: process categories and CPU seconds between two times."""
import json
import os

from tpch.procmon import category, sample, window


def test_category_by_comm():
    assert category(str(os.getpid()), "python3") == "monitor"
    assert category("1", "kswapd0") == "kswapd"
    assert category("1", "kworker/u64:2") == "kworker"
    assert category("1", "replica_pool") == "replica"
    assert category("1", "bash") == "other"


def test_window_takes_the_nearest_samples(tmp_path):
    f = tmp_path / "pm.jsonl"
    rows = [{"t": 100.0, "cpu": {"kswapd": 1.0}}, {"t": 101.0, "cpu": {"kswapd": 1.5, "io_worker_0": 2.0}},
            {"t": 102.0, "cpu": {"kswapd": 3.0, "io_worker_0": 5.0}}]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    w = window(f, 100.1, 101.9)
    assert w == {"kswapd": 2.0, "io_worker_0": 5.0}                 # a category absent at the start counts from 0


def test_sample_accumulates_deltas():
    state, total = {}, {}
    sample(state, total)                  # first sight of every process: no delta yet
    assert total == {}
    sample(state, total)
    assert all(v >= 0 for v in total.values())
