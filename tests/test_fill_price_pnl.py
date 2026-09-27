"""Task B: legs_gross_notional prefers fill_price, falls back to price."""
from __future__ import annotations

import pytest

from backend.strategy.pnl import legs_gross_notional


def test_gross_uses_fill_price_when_present():
    legs = [
        {"qty": 2.0, "price": 100.0, "fill_price": 110.0},
        {"qty": 1.0, "price": 50.0, "fill_price": 48.0},
    ]
    assert legs_gross_notional(legs) == pytest.approx(268.0)


def test_gross_falls_back_to_price():
    legs = [
        {"qty": 2.0, "price": 100.0},
        {"filled_qty": 3.0, "price": 20.0},
    ]
    assert legs_gross_notional(legs) == pytest.approx(260.0)


def test_gross_mixed_fill_and_decision():
    legs = [
        {"qty": 1.0, "price": 100.0, "fill_price": 105.0},
        {"qty": 2.0, "price": 50.0},
    ]
    assert legs_gross_notional(legs) == pytest.approx(105.0 + 100.0)
