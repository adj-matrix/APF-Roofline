import pytest

from queries.q3.model import HashTable, io_seconds, probe_ns, probe_ns_split, warm_seconds_scaled


def test_hash_table_layout():
    ht = HashTable(tuples=1000, data_width=12)          # 16 + maxalign(16 + 12) = 48 B per tuple
    assert ht.buckets == 1024 and ht.alpha == pytest.approx(1000 / 1024)
    assert ht.bytes == 1000 * 48 + 1024 * 8


def test_batches_double_until_it_fits():
    ht = HashTable(tuples=1 << 20, data_width=12)       # 48 MiB + 8 MiB = 56 MiB
    assert ht.batches(128 << 20) == 1
    assert ht.batches(16 << 20) == 4


def test_probe_cost_counts_bucket_plus_chain():
    ht = HashTable(tuples=512, data_width=4)            # alpha 1.0
    assert ht.loads_per_probe() == pytest.approx(2.0)
    assert probe_ns(1000, ht, 100.0) == pytest.approx(200_000)


def test_io_seconds_per_table():
    assert io_seconds({"a": (100.0, 50.0), "b": (30.0, 10.0)}) == {"a": 2.0, "b": 3.0}


def test_warm_scaling_without_and_with_hash_wait():
    # 3e9 cycles at 3 GHz = 1 s, of which 0.1 s (0.3e9 cycles) waiting for the hash table
    linear = warm_seconds_scaled(3e9, 3.0, 0.1e9, 10, 3.0, 1.0e9)
    assert linear == pytest.approx(10.0)                # waits scale linearly too: 9 + 1
    slower_memory = warm_seconds_scaled(3e9, 3.0, 0.1e9, 10, 3.0, 4.0e9)
    assert slower_memory == pytest.approx(13.0)         # 9 s compute + 4 s waiting for DRAM


def test_split_probe_uses_each_working_set():
    ht = HashTable(tuples=512, data_width=12)            # 512 buckets (4 KiB), alpha 1, tuples 24 KiB
    lat = lambda size: 10.0 if size <= 4096 else 100.0   # noqa: E731
    assert ht.bucket_bytes == 4096 and ht.tuple_bytes == 512 * 48
    assert probe_ns_split(10, ht, lat) == pytest.approx(10 * (10.0 + 1.0 * 100.0))
