import numpy as np
import pandas as pd
import pytest

from backend.engine.backtester import backtest_group
from backend.strategy.liquidation import (
    legs_from_betas, liq_price, max_safe_leverage, near_liquidation, nearest_liquidation,
)
from backend.strategy.sizing import leg_orders


def test_liq_price_isolated():
    assert liq_price(100, "buy", 2, 0.005) == pytest.approx(50.5)
    assert liq_price(100, "sell", 2, 0.005) == pytest.approx(149.5)
    assert liq_price(100, "sell", 10, 0.005) == pytest.approx(109.5)
    assert liq_price(100, "buy", 1, 0.005) == pytest.approx(0.5)


def test_near_liquidation_band_and_past_liquidation():
    legs = [{"symbol": "A", "side": "sell", "price": 100.0}]
    # x10 short: liq 109.5; mark 100 -> 9.5% away: inside a 20% band
    assert near_liquidation(legs, {"A": 100.0}, 10, 0.2)["symbol"] == "A"
    # x2 short: liq 149.5; mark 100 -> 49.5% away: outside
    assert near_liquidation(legs, {"A": 100.0}, 2, 0.2) is None
    # mark already beyond liquidation is always "near"
    assert nearest_liquidation(legs, {"A": 160.0}, 2)["dist"] < 0
    assert near_liquidation(legs, {"A": 160.0}, 2, 0.01) is not None
    # guard off
    assert near_liquidation(legs, {"A": 100.0}, 10, 0) is None


def test_max_safe_leverage_matches_entry_check():
    thr, mmr = 0.2, 0.005
    lim = max_safe_leverage(thr, mmr)
    assert 4.8 < lim < 4.9
    legs = [{"symbol": "A", "side": "buy", "price": 100.0}]
    assert near_liquidation(legs, {"A": 100.0}, lim * 0.99, thr, mmr) is None
    assert near_liquidation(legs, {"A": 100.0}, lim * 1.01, thr, mmr) is not None


def test_legs_from_betas_matches_live_sizing_sides():
    betas, prices = {"X": 0.9, "Z": -0.4}, {"Y": 100.0, "X": 50.0, "Z": 30.0}
    for direction in ("long_residual", "short_residual"):
        ours = {l["symbol"]: l["side"] for l in legs_from_betas("Y", direction, betas, prices)}
        live = {l["symbol"]: l["side"] for l in leg_orders("Y", direction, betas, prices, 1000.0)}
        assert ours == live


def _synthetic(n=900, seed=3):
    rng = np.random.default_rng(seed)
    x = 100 + np.cumsum(rng.normal(size=n))
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = 0.8 * spread[i - 1] + rng.normal()
    y = 1.5 * x + 20 + spread
    idx = pd.date_range("2026-01-01", periods=n, freq="min")
    return pd.DataFrame({"X": x, "Y": y}, index=idx)


KW = dict(window_size=100, adf_alpha=0.05, kpss_alpha=0.05, z_entry=2.0, z_close=0.5,
          z_stop_loss=6.0, transaction_cost_rate=0.0001)


def test_backtest_guard_exits_with_risk_exit():
    df = _synthetic()
    base = backtest_group(df, "Y", **KW)
    assert base.performance.trade_count > 0
    assert all(t.close_reason != "risk_exit" for t in base.trades)
    # x4.5 leaves a ~21.7% cushion at entry (entry allowed) but a ~2% adverse move trips the 20% guard
    guarded = backtest_group(df, "Y", leverage=4.5, liquidation_proximity=0.2, **KW)
    assert any(t.close_reason == "risk_exit" for t in guarded.trades)


def test_backtest_skips_entries_that_start_inside_the_band():
    df = _synthetic()
    assert backtest_group(df, "Y", **KW).performance.trade_count > 0
    # x10 -> cushion 9.5% < 20%: the engine would never open these, so neither does the backtest
    res = backtest_group(df, "Y", leverage=10, liquidation_proximity=0.2, **KW)
    assert res.performance.trade_count == 0


def test_backtest_unchanged_when_guard_off_or_leverage_low():
    df = _synthetic()
    base = backtest_group(df, "Y", **KW)
    off = backtest_group(df, "Y", leverage=10, liquidation_proximity=0, **KW)
    low = backtest_group(df, "Y", leverage=1, liquidation_proximity=0.2, **KW)
    key = lambda r: [(t.entry_idx, t.exit_idx, t.close_reason) for t in r.trades]
    assert key(off) == key(base)
    assert key(low) == key(base)


def test_config_validation_of_guard_keys():
    from backend import config_service as cs
    base = {"z_entry": 2, "z_close": 0.5, "z_stop_loss": 3.5, "trade_notional": 10}
    cs._validate_backbone({**base, "liquidation_proximity_fraction": 0.2, "maintenance_margin_rate": 0.005})
    with pytest.raises(ValueError):
        cs._validate_backbone({**base, "liquidation_proximity_fraction": 1.5})
    with pytest.raises(ValueError):
        cs._validate_backbone({**base, "maintenance_margin_rate": -0.1})


def _shocked_frame(direction_factor):
    """Common-factor shock that leaves the residual (and z) untouched but moves every leg's price,
    starting 2 bars after the first baseline entry."""
    from backend.strategy.ols import fit_ols
    rng = np.random.default_rng(11)
    n = 1500
    x = 60000 * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
    ou = np.zeros(n)
    for i in range(1, n):
        ou[i] = 0.88 * ou[i - 1] + rng.normal(0, 0.004)
    spread = 25 * ou / ou.std()
    idx = pd.date_range("2026-01-01", periods=n, freq="60s")
    kw = dict(window_size=100, adf_alpha=0.05, kpss_alpha=0.05, z_entry=2.0, z_close=0.5,
              z_stop_loss=3.5, transaction_cost_rate=0.0011, half_life_max_fraction=0.333)
    base = pd.DataFrame({"Y": 0.04 * x + spread, "X": x}, index=idx)
    t0 = backtest_group(base, "Y", **kw).trades[0]
    fit = fit_ols("Y", {"Y": base.Y.to_numpy()[t0.entry_idx - 100:t0.entry_idx],
                        "X": x[t0.entry_idx - 100:t0.entry_idx]})
    beta = float(list(fit.betas.values())[0])
    at = t0.entry_idx + 2
    dx = np.zeros(n)
    dx[at:at + 10] = np.linspace(0, (direction_factor - 1) * x[at], 10)
    dx[at + 10:] = (direction_factor - 1) * x[at]
    df = pd.DataFrame({"Y": 0.04 * x + beta * dx + spread, "X": x + dx}, index=idx)
    return df, kw


@pytest.mark.parametrize("factor", [1.35, 0.5])
def test_guard_fires_on_adverse_leg_moves_in_both_directions_and_only_when_close(factor):
    df, kw = _shocked_frame(factor)
    first = lambda **k: backtest_group(df, "Y", **k, **kw).trades[0]
    assert first().close_reason == "close"                                              # no guard
    assert first(leverage=4.5, liquidation_proximity=0.2).close_reason == "risk_exit"   # 21.7% cushion
    assert first(leverage=4.5, liquidation_proximity=0.0).close_reason == "close"       # guard off
    assert first(leverage=1, liquidation_proximity=0.2).close_reason == "close"         # no leverage
