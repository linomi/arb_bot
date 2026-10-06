import numpy as np
import pandas as pd
import pytest

from backend.strategy.stats_tests import test_stationarity
from backend.strategy.ols import fit_ols
from backend.strategy.zscore import decide_entry
from backend.strategy.metrics import compute_group_performance
from backend.engine.backtester import backtest_group
from backend.engine.bot_engine import _fill_price_from_position, BotEngine
from backend import config_service


def _stat(y, x, method):
    pm = {"Y": y, "X": x}
    fit = fit_ols("Y", pm)
    return test_stationarity(fit.residual, 0.05, 0.05, y=y, x_cols=[x], method=method)


def test_engle_granger_far_less_lenient_than_adf_on_random_walks():
    rng = np.random.default_rng(1)
    eg = adf = 0
    n = 150
    for _ in range(n):
        y = np.cumsum(rng.normal(size=100)) + 100
        x = np.cumsum(rng.normal(size=100)) + 100
        eg += _stat(y, x, "engle_granger").passed
        adf += _stat(y, x, "adf_kpss").passed
    assert eg / n < 0.12          # near nominal
    assert adf > eg               # legacy test passes more spurious pairs


def test_engle_granger_accepts_true_cointegration():
    rng = np.random.default_rng(2)
    x = np.cumsum(rng.normal(size=200)) + 100
    y = 2 * x + 5 + rng.normal(size=200)
    assert _stat(y, x, "engle_granger").passed


def test_decide_entry_refuses_beyond_stop():
    d = decide_entry(10.0, 0.0, 1.0, 2.0, z_stop=3.5)
    assert not d.should_enter and d.reason == "beyond_stop"
    assert decide_entry(2.5, 0.0, 1.0, 2.0, z_stop=3.5).should_enter
    assert decide_entry(-2.5, 0.0, 1.0, 2.0, z_stop=3.5).direction == "long_residual"


def test_sortino_penalises_steady_small_losses():
    now = pd.Timestamp("2026-01-01")
    trades = [{"pnl": p, "entry_time": now, "close_time": now + pd.Timedelta(hours=1)}
              for p in (-1.0, -1.0, -1.0, 0.5)]
    perf = compute_group_performance(trades)
    assert perf.sortino_ratio < 0


def test_close_fill_price_never_uses_entry_price():
    pos = {"entryPrice": "100", "exitPrice": None, "markPrice": None}
    assert _fill_price_from_position(pos, is_close=True) is None
    assert _fill_price_from_position({**pos, "markPrice": "90"}, is_close=True) == 90.0
    assert _fill_price_from_position({**pos, "exitPrice": "95"}, is_close=True) == 95.0
    assert _fill_price_from_position(pos, is_close=False) == 100.0


def _synthetic(n=600, seed=3):
    rng = np.random.default_rng(seed)
    x = 100 + np.cumsum(rng.normal(size=n))
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = 0.8 * spread[i - 1] + rng.normal()
    y = 1.5 * x + 20 + spread
    idx = pd.date_range("2026-01-01", periods=n, freq="min")
    return pd.DataFrame({"X": x, "Y": y}, index=idx)


def test_backtest_runs_and_marks_open_trade_to_market():
    df = _synthetic()
    kw = dict(window_size=100, adf_alpha=0.05, kpss_alpha=0.05, z_entry=2.0,
              z_close=0.5, z_stop_loss=3.5, transaction_cost_rate=0.0001)
    res = backtest_group(df, "Y", **kw)
    assert res.performance.trade_count > 0
    # a time stop of 1 bar must close everything with reason time_stop
    ts = backtest_group(df, "Y", max_holding_bars=1, **kw)
    assert any(t.close_reason == "time_stop" for t in ts.trades)
    # open_mtm only appears when an open trade is left at the end
    nomtm = backtest_group(df, "Y", mark_open_at_end=False, **kw)
    assert len(res.trades) - len(nomtm.trades) in (0, 1)
    assert all(t.close_reason != "open_mtm" for t in nomtm.trades)


def test_entry_cooldown():
    eng = BotEngine()
    assert not eng._in_cooldown(1)
    eng._start_cooldown(1, {"entry_retry_cooldown_sec": 600})
    assert eng._in_cooldown(1) and not eng._in_cooldown(2)
    eng2 = BotEngine()
    eng2._start_cooldown(1, {"entry_retry_cooldown_sec": 0})
    assert not eng2._in_cooldown(1)


def test_config_validation_new_keys():
    base = {"z_entry": 2, "z_close": 0.5, "z_stop_loss": 3.5, "trade_notional": 10,
            "window_size": 100, "sampling_time": 60}
    config_service._validate_backbone({**base, "stationarity_method": "engle_granger"})
    with pytest.raises(ValueError):
        config_service._validate_backbone({**base, "stationarity_method": "bogus"})
    with pytest.raises(ValueError):
        config_service._validate_backbone({**base, "max_holding_hours": -1})
