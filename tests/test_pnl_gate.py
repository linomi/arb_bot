"""Unit tests for profit-target entry gate (pnl.py)."""
from __future__ import annotations

import pytest

from backend.strategy.pnl import gross_per_unit_y, entry_target_check


def test_gross_per_unit_y_example():
    # P_y=100, β=2, P_x=10 -> G = 100 + 2*10 = 120 (not 300)
    prices = {"Y": 100.0, "X": 10.0}
    betas = {"X": 2.0}
    assert gross_per_unit_y(prices, "Y", betas) == pytest.approx(120.0)


def test_gross_per_unit_y_multiple_legs():
    prices = {"Y": 50.0, "A": 20.0, "B": 5.0}
    betas = {"A": 1.5, "B": -0.5}
    # 50 + 1.5*20 + 0.5*5 = 50 + 30 + 2.5 = 82.5
    assert gross_per_unit_y(prices, "Y", betas) == pytest.approx(82.5)


def test_entry_target_disabled():
    ok, d = entry_target_check(
        z_now=1.0, sigma=1.0, z_close=0.5, z_stop=3.5,
        gross_per_unit_y=100.0, cost_rate=0.0015, target_rate=0.0,
    )
    assert ok is True
    assert d["disabled"] is True


def test_entry_target_pass():
    # cost_rate=0.0015, target=0.01, z_close=0.5, z_stop=3.5, sigma_rel=0.0087
    # z_min ≈ 0.5 + (0.003 + 0.01)/0.0087 ≈ 0.5 + 1.494 ≈ 1.994 ≈ 2.0
    G = 1000.0
    sigma = 0.0087 * G
    ok, d = entry_target_check(
        z_now=2.0, sigma=sigma, z_close=0.5, z_stop=3.5,
        gross_per_unit_y=G, cost_rate=0.0015, target_rate=0.01,
    )
    assert ok is True
    assert d["z_min"] == pytest.approx(2.0, abs=0.05)
    assert d["net_frac"] >= 0.01


def test_entry_target_reject_net_below():
    G = 1000.0
    sigma = 0.0087 * G
    ok, d = entry_target_check(
        z_now=1.0, sigma=sigma, z_close=0.5, z_stop=3.5,
        gross_per_unit_y=G, cost_rate=0.0015, target_rate=0.01,
    )
    assert ok is False
    assert d["reject_reason"] == "net_below_target"


def test_entry_target_reject_near_stop():
    # sigma_rel=0.004 -> z_min ≈ 0.5 + 0.013/0.004 = 3.75
    G = 1000.0
    sigma = 0.004 * G
    ok, d = entry_target_check(
        z_now=4.0, sigma=sigma, z_close=0.5, z_stop=3.5,
        gross_per_unit_y=G, cost_rate=0.0015, target_rate=0.01,
    )
    assert ok is False
    assert d["z_min"] == pytest.approx(3.75, abs=0.05)
    assert d["reject_reason"] == "z_min_near_stop"


def test_entry_target_invalid_sigma():
    ok, d = entry_target_check(
        z_now=2.0, sigma=0.0, z_close=0.5, z_stop=3.5,
        gross_per_unit_y=100.0, cost_rate=0.0015, target_rate=0.01,
    )
    assert ok is False
    assert d["reject_reason"] == "invalid_G_or_sigma"
