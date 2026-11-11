from tpch.counters import delta, parse_diskstats, parse_pid_io, parse_pid_stat

DISKSTATS = """\
   8       0 sda 1000 10 204800 500 20 2 160 30 0 520 530 0 0 0 0 0 0
 253       2 dm-2 990 0 204000 498 22 0 176 31 0 519 529 0 0 0 0 0 0
"""


def test_parse_diskstats_selects_device():
    d = parse_diskstats(DISKSTATS, "sda")
    assert d["reads"] == 1000 and d["sectors_read"] == 204800 and d["io_ticks_ms"] == 520
    assert parse_diskstats(DISKSTATS, "dm-2")["reads"] == 990


def test_parse_pid_stat_handles_spaces_in_comm():
    # pid (comm) state ppid ... ; utime/stime are overall fields 14/15
    fields = ["S", "1"] + ["0"] * 9 + ["250", "40"] + ["0"] * 30
    stat = "4242 (postgres: me tpch_sf10 [local] SELECT) " + " ".join(fields)
    p = parse_pid_stat(stat)
    assert p == {"utime_s": 2.5, "stime_s": 0.4}   # CLK_TCK = 100 on Linux


def test_parse_pid_io():
    io = "rchar: 900\nwchar: 1\nsyscr: 2\nsyscw: 3\nread_bytes: 4096\nwrite_bytes: 0\n"
    assert parse_pid_io(io) == {"read_bytes": 4096, "rchar": 900}


def test_delta():
    a = {"disk": {"reads": 1}, "proc": {"utime_s": 1.0}}
    b = {"disk": {"reads": 5}, "proc": {"utime_s": 3.5}}
    assert delta(a, b) == {"disk": {"reads": 4}, "proc": {"utime_s": 2.5}}
    assert delta(a, {"disk": {"reads": 2}}) == {"disk": {"reads": 1}}   # proc missing after
