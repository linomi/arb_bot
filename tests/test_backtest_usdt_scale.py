import numpy as np
import pandas as pd

from backend.engine.backtester import backtest_group


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


def test_usdt_backtest_is_not_blocked_by_the_irt_minimum_with_max_entry_scale():
    r = backtest_group(
        price_df=_df(), dependent_symbol="ETH/USDT:USDT", window_size=100, adf_alpha=0.05, kpss_alpha=0.05,
        z_entry=2.0, z_close=0.5, z_stop_loss=3.5, transaction_cost_rate=0.0015,
        max_entry_scale=3.0, trade_notional=100.0,
    )
    assert len(r.trades) > 0
