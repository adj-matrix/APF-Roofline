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
