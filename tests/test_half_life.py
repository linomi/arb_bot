import numpy as np
from backend.strategy.half_life import residual_half_life_bars, half_life_ok


def _ar(phi, n=500, seed=0, noise=0.01):
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + rng.normal() * noise
    return r


def test_fast_mean_reversion_has_short_half_life():
    hl = residual_half_life_bars(_ar(0.5, noise=1.0))
    assert 0.5 < hl < 3.0


def test_explosive_series_infinite_half_life():
    assert residual_half_life_bars(_ar(1.05, noise=0.01)) == float("inf")


def test_half_life_ok_window_bounds():
    ok, hl = half_life_ok(_ar(0.7), window_size=100, max_fraction=1 / 3, min_bars=1.0)
    assert ok and 1.0 < hl < 100 / 3
    ok2, hl2 = half_life_ok(_ar(0.99), window_size=60, max_fraction=1 / 3)
    assert not ok2 and hl2 > 20


def test_too_fast_rejected_by_min_bars():
    ok, hl = half_life_ok(_ar(0.3, noise=1.0), window_size=100, min_bars=2.0)
    assert not ok and hl < 2
