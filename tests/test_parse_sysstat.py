"""Parsers against real output captured with tpch.monitor commands (tests/fixtures/*.log)."""
from pathlib import Path

from tpch.parse.sysstat import parse_iostat, parse_mpstat, parse_pidstat

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text()


def test_iostat_rows_and_columns():
    rows = parse_iostat(read("iostat.log"), device="sda")
    assert len(rows) == 4
    r = rows[0]
    for col in ("r/s", "rkB/s", "rareq-sz", "r_await", "w/s", "wkB/s", "aqu-sz", "%util"):
        assert isinstance(r[col], float), col
    assert r["device"] == "sda"
    assert [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])] == [1.0, 1.0, 1.0]


def test_iostat_device_filter():
    assert parse_iostat(read("iostat.log"), device="nvme0n1") == []
    assert len(parse_iostat(read("iostat.log"))) == 4  # only sda was sampled


def test_mpstat_all_cpus_and_skips_average():
    rows = parse_mpstat(read("mpstat.log"))
    assert len(rows) == 4 * 33  # 4 samples x ("all" + 32 CPUs)
    alls = [r for r in rows if r["CPU"] == "all"]
    assert len(alls) == 4
    assert {"%usr", "%sys", "%iowait", "%idle"} <= alls[0].keys()
    assert abs(sum(alls[0][k] for k in ("%usr", "%nice", "%sys", "%iowait", "%irq",
                                        "%soft", "%steal", "%guest", "%gnice", "%idle")) - 100) < 0.1


def test_mpstat_midnight_rollover():
    text = (
        "Linux 5.15.0 (host) \t2026-11-06 \t_x86_64_\t(2 CPU)\n\n"
        "23:59:59     CPU    %usr   %idle\n"
        "23:59:59     all    1.00   99.00\n"
        "00:00:00     CPU    %usr   %idle\n"
        "00:00:00     all    2.00   98.00\n"
    )
    a, b = parse_mpstat(text)
    assert b["ts"] - a["ts"] == 1.0


def test_pidstat_epoch_and_columns():
    rows = parse_pidstat(read("pidstat.log"))
    assert len(rows) == 4
    assert {"%usr", "%system", "%CPU", "kB_rd/s", "iodelay", "PID"} <= rows[0].keys()
    assert rows[0]["Command"] == "postgres"


def test_all_samplers_share_one_clock():
    """The three tools must agree on time so they can be joined into one timeline."""
    io = parse_iostat(read("iostat.log"))[0]["ts"]
    mp = parse_mpstat(read("mpstat.log"))[0]["ts"]
    pid = parse_pidstat(read("pidstat.log"))[0]["ts"]
    assert max(io, mp, pid) - min(io, mp, pid) <= 1.0
