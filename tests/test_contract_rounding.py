"""Whole-contract rounding (XT): shared sizing, drift gate, backtest report, engine fit."""
import asyncio

import numpy as np
import pandas as pd
import pytest

from backend.engine.backtester import backtest_group
from backend.engine.bot_engine import BotEngine
from backend.strategy.pnl import legs_gross_notional
from backend.strategy.sizing import ContractRoundingError, drift_gate, leg_orders, rounding_info

A, B = "ETH/USDT:USDT", "BTC/USDT:USDT"
PR = {A: 1.3251, B: 0.4931}
CS = {A: 10.0, B: 1.0}
BETA = {B: 0.8}


def _legs(notional, mode="joint", cs=CS, **kw):
    return leg_orders(A, "long_residual", BETA, PR, notional, min_order_value=5.0,
                      contract_sizes=cs, contract_mode=mode, **kw)


def test_no_contract_sizes_leaves_legs_continuous():
    legs = leg_orders(A, "long_residual", BETA, PR, 60.0, min_order_value=5.0)
    assert rounding_info(legs) is None and "contracts" not in legs[0]


@pytest.mark.parametrize("mode", ["floor", "nearest", "joint"])
def test_every_leg_is_a_whole_number_of_contracts(mode):
    for l in _legs(60.0, mode):
        k = l["qty"] / CS[l["symbol"]]
        assert k >= 1 and abs(k - round(k)) < 1e-9
        assert l["contracts"] == round(k)


def test_joint_keeps_hedge_closer_than_nearest_or_floor():
    drift = {m: rounding_info(_legs(60.0, m))["hedge_drift"] for m in ("floor", "nearest", "joint")}
    assert drift["joint"] <= min(drift["floor"], drift["nearest"]) + 1e-12
    assert drift["joint"] < 0.02  # 3 ETH-like contracts vs 24 BTC-like contracts is an exact 1:8 hedge


def test_total_size_stays_inside_the_allowed_band():
    info = rounding_info(_legs(60.0, "joint", contract_max_size_dev=0.35))
    assert abs(info["size_ratio"] - 1.0) <= 0.35 + 1e-9


def test_leg_below_one_contract_is_infeasible_with_clear_reason():
    with pytest.raises(ContractRoundingError) as e:
        leg_orders(A, "long_residual", BETA, PR, 12.0, min_order_value=5.0,
                   contract_sizes={A: 10.0, B: 1.0}, contract_mode="nearest")
    assert "contract" in str(e.value)


def test_unknown_contract_size_leg_is_left_continuous():
    legs = _legs(60.0, "joint", cs={A: 10.0})
    b = next(l for l in legs if l["symbol"] == B)
    assert "contracts" not in b


def test_drift_gate_hedge_cap_and_noise_vs_edge():
    info = {"hedge_drift": 0.30, "drift_notional": 10.0}
    ok, det = drift_gate(info, expected_net_profit=5.0, max_drift=0.15)
    assert not ok and "hedge drift" in det["reject_reason"]
    ok, det = drift_gate({"hedge_drift": 0.05, "drift_notional": 100.0}, expected_net_profit=0.5,
                         max_drift=0.15, move_estimate=0.01, max_noise_ratio=0.5)
    assert not ok and "noise" in det["reject_reason"]        # noise 1.0 > 0.25
    ok, _ = drift_gate({"hedge_drift": 0.05, "drift_notional": 10.0}, expected_net_profit=0.5,
                       max_drift=0.15, move_estimate=0.01, max_noise_ratio=0.5)
    assert ok
    ok, _ = drift_gate({"hedge_drift": 0.9, "drift_notional": 1e9}, expected_net_profit=0.5,
                       max_drift=0, max_noise_ratio=0)           # both checks off
    assert ok and drift_gate(None, expected_net_profit=1.0)[0]


def test_contract_to_base_conversion_is_float_safe():
    from unittest.mock import AsyncMock, MagicMock, patch
    with patch("backend.exchange.xt.ccxt") as mod:
        ex = AsyncMock()
        ex.markets = {A: {"contractSize": 0.1}}
        ex.market = lambda s: ex.markets[s]
        ex.amount_to_precision = lambda s, a: str(int(a))   # truncating precision, like ccxt
        mod.xt = MagicMock(return_value=ex)
        from backend.exchange.xt import XTClient
        c = XTClient(api_key="k", api_secret="s")
        for k in (1, 3, 7, 29, 58):
            assert c._base_to_contracts(A, k * 0.1) == k


def _df():
    rng = np.random.default_rng(7)
    n = 2500
    x = 60000 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
    ou = np.zeros(n)
    for i in range(1, n):
        ou[i] = 0.88 * ou[i - 1] + rng.normal(0, 0.004)
    y = 0.04 * x + 25 * ou / ou.std()
    return pd.DataFrame({"ETH/USDT:USDT": y, "BTC/USDT:USDT": x},
                        index=pd.date_range("2026-01-01", periods=n, freq="60s"))


def _bt(rounding, notional=500.0):
    return backtest_group(
        price_df=_df(), dependent_symbol="ETH/USDT:USDT", window_size=100, adf_alpha=0.05, kpss_alpha=0.05,
        z_entry=2.0, z_close=0.5, z_stop_loss=3.5, transaction_cost_rate=0.0015,
        trade_notional=notional, rounding=rounding)


def _cfg(cs, **kw):
    base = {"contract_sizes": cs, "mode": "joint", "max_size_dev": 0.35, "max_drift": 0.15,
            "move": 0.01, "noise_ratio": 0.5}
    base.update(kw)
    return base


def test_backtest_without_rounding_has_no_report():
    r = _bt(None)
    assert r.rounding is None and all(t.pnl_ideal is None for t in r.trades)


def test_backtest_reports_rounded_vs_ideal_and_drift():
    r = _bt(_cfg({"ETH/USDT:USDT": 0.01, "BTC/USDT:USDT": 0.0001}))
    rep = r.rounding
    assert rep and rep["enabled"] and rep["entries"] > 0
    assert rep["trades"] == len(r.trades) and rep["hedge_drift_max"] is not None
    assert all(t.pnl_ideal is not None for t in r.trades)
    # tiny contracts => rounding is nearly free
    assert abs(rep["pnl_lost_to_drift_usdt"]) < 0.05 * max(1.0, abs(rep["pnl_ideal_usdt"]))
    assert rep["pnl_lost_to_rounding_usdt"] == pytest.approx(rep["pnl_lost_to_size_usdt"] + rep["pnl_lost_to_drift_usdt"])


def test_backtest_skips_entries_that_cannot_be_sized_in_contracts():
    # one BTC-like contract (60 USDT of price x 1000) is far above the 500 notional: nothing can be opened
    r = _bt(_cfg({"ETH/USDT:USDT": 1.0, "BTC/USDT:USDT": 1000.0}))
    assert len(r.trades) == 0 and r.rounding["skipped_rounding"] > 0


def test_engine_fit_uses_whole_contracts_in_paper_mode():
    class C:
        async def get_min_notional(self, s):
            return 5.0
    legs, n, msg = asyncio.run(BotEngine()._fit_notional_to_balance(
        C(), dependent_symbol=A, direction="long_residual", betas=BETA, prices=PR, desired_notional=60.0,
        trading_mode="paper", exchange="xt", symbols=[A, B],
        rounding={"contract_sizes": CS, "mode": "joint", "max_size_dev": 0.35}))
    assert legs and all(abs(l["qty"] / CS[l["symbol"]] - round(l["qty"] / CS[l["symbol"]])) < 1e-9 for l in legs)
    assert rounding_info(legs)["hedge_drift"] < 0.02
    assert legs_gross_notional(legs) == pytest.approx(sum(l["qty"] * l["price"] for l in legs))


def test_engine_fit_reports_infeasible_rounding_without_raising():
    class C:
        async def get_min_notional(self, s):
            return 5.0
    legs, n, msg = asyncio.run(BotEngine()._fit_notional_to_balance(
        C(), dependent_symbol=A, direction="long_residual", betas=BETA, prices=PR, desired_notional=12.0,
        trading_mode="paper", exchange="xt", symbols=[A, B],
        rounding={"contract_sizes": CS, "mode": "nearest", "max_size_dev": 0.35}))
    assert legs is None and msg.startswith("contract rounding")
