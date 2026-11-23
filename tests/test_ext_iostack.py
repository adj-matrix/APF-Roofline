"""E1 (tpch.ext.iostack): the clock fixed point of the serialized sync / direct cell."""
from tpch.ext.iostack.model import _fixed_point


def test_fixed_point_on_flat_curve_is_the_curve():
    f, d = _fixed_point(100e9, 50.0, [[0.0, 2.0], [1.0, 2.0]])
    assert abs(f - 2.0) < 1e-9 and abs(d - 50 / 100) < 1e-9          # cpu = 100e9 / 2e9 = 50 s


def test_fixed_point_slower_clock_lengthens_cpu_and_raises_share():
    # the clock falls with the on-CPU share: the fixed point sits between the two ends and is self-consistent
    curve = [[0.5, 1.0], [1.0, 3.0]]
    f, d = _fixed_point(150e9, 50.0, curve)
    cpu = 150e9 / (f * 1e9)
    assert abs(d - cpu / (cpu + 50.0)) < 1e-6
    assert 1.0 < f < 3.0
    assert abs(f - (1.0 + (d - 0.5) * 4.0)) < 1e-4


def test_scan_family_points_and_off_cpu_time():
    # model v3: one point per TPC-H-free scan (medians over its runs), off-CPU time per read from wall and cycles
    from tpch.ext.iostack.model_v3 import family
    row = lambda u, wall: {"u": u, "k": 10e3, "f": 2e9, "wall": wall, "n": 1000.0}
    scans = {"lineitem_mid": [row(900e3, 2.0), row(900e3, 2.2)], "lineitem_min": [row(300e3, 1.0)]}
    pts = family(scans, start=0.5)
    assert [p["scan"] for p in pts] == ["lineitem_min", "lineitem_mid"]          # sorted by user cycles per read
    assert pts[1]["runs"] == 2 and abs(pts[1]["wall"] - 2.1) < 1e-12              # median of the runs
    # off per read = (wall - start) / n - (u + k) / f
    assert abs(pts[0]["off"] - ((1.0 - 0.5) / 1000 - 310e3 / 2e9)) < 1e-15
