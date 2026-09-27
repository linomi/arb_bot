"""Unit tests for leg_orders max_scale guard (Task C) and min-order sizing."""
from __future__ import annotations

import pytest

from backend.strategy.sizing import (
    leg_orders,
    ExcessiveScalingError,
    MIN_ORDER_VALUE_IRT,
)


def test_leg_orders_no_max_scale_unchanged():
    prices = {"YIRT": 10_000.0, "XIRT": 5_000.0}
    betas = {"XIRT": 1.0}
    legs = leg_orders("YIRT", "long_residual", betas, prices, 100_000.0, max_scale=None)
    legs2 = leg_orders("YIRT", "long_residual", betas, prices, 100_000.0)
    assert [l["qty"] for l in legs] == [l["qty"] for l in legs2]


def test_leg_orders_under_max_scale_passes():
    prices = {"YIRT": 10_000.0, "XIRT": 5_000.0}
    betas = {"XIRT": 1.0}
    legs = leg_orders(
        "YIRT", "long_residual", betas, prices, 200_000.0, max_scale=3.0,
    )
    scales = [l.get("scale", 1.0) for l in legs]
    assert max(scales) <= 3.0
    for leg in legs:
        assert float(leg["qty"]) * float(leg["price"]) >= MIN_ORDER_VALUE_IRT * 0.99


def test_leg_orders_over_max_scale_raises():
    prices = {"YIRT": 1_000_000.0, "XIRT": 50_000.0}
    betas = {"XIRT": 0.01}
    with pytest.raises(ExcessiveScalingError) as ei:
        leg_orders(
            "YIRT", "long_residual", betas, prices, 1_000.0, max_scale=2.0,
        )
    assert ei.value.scale > 2.0


def test_leg_orders_qty_times_price_min_order_ballpark():
    prices = {"YIRT": 100_000.0}
    legs = leg_orders("YIRT", "long_residual", {}, prices, 30_000.0)
    assert len(legs) == 1
    notional = float(legs[0]["qty"]) * float(legs[0]["price"])
    assert notional >= MIN_ORDER_VALUE_IRT * 0.99
    assert legs[0].get("scale", 1) >= 1.0
