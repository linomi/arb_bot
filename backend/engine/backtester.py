"""
Bar-by-bar replay of the backbone logic (steps 1-5) over historical OHLC
closes. Used by:
  - init-time backtesting/pruning for both initialization methods
  - (optionally) manual "backtest this group" calls from the UI

PnL model: unit-notional log-spread return per trade, minus transaction
costs (fee + slippage) applied per leg, per side (entry+exit). This is an
approximation appropriate for ranking/pruning candidate groups -- it is NOT
a substitute for realistic position sizing, which the live/paper engine
handles separately per-order.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd

from backend.strategy.ols import fit_ols
from backend.strategy.stats_tests import test_stationarity
from backend.strategy.zscore import zscore, decide_entry, decide_exit
from backend.strategy.metrics import compute_group_performance, GroupPerformance


@dataclass
class BacktestTrade:
    entry_idx: int
    exit_idx: int
    entry_time: pd.Timestamp
    close_time: pd.Timestamp
    direction: str
    entry_z: float
    close_z: float
    close_reason: str
    pnl: float


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    performance: GroupPerformance


def simulate_pnl(entry_residual: float, exit_residual: float, direction: str,
                  num_legs: int, transaction_cost_rate: float) -> float:
    raw = (entry_residual - exit_residual) if direction == "short_residual" else (exit_residual - entry_residual)
    cost = transaction_cost_rate * num_legs * 2  # entry + exit, one cost hit per leg each side
    return raw - cost


def backtest_group(
    price_df: pd.DataFrame,
    dependent_symbol: str,
    window_size: int,
    adf_alpha: float,
    kpss_alpha: float,
    z_entry: float,
    z_close: float,
    z_stop_loss: float,
    transaction_cost_rate: float,
) -> BacktestResult:
    """
    price_df: DataFrame indexed by timestamp, one column per symbol in the
    group (including dependent_symbol), already aligned/resampled to the
    sampling interval.
    """
    symbols = list(price_df.columns)
    num_legs = len(symbols)
    n = len(price_df)
    trades: list[BacktestTrade] = []

    position = None  # dict with direction, betas, intercept, mean, std, entry_idx, entry_time, entry_residual

    i = window_size
    while i < n:
        prices_now = price_df.iloc[i].to_dict()

        if position is not None:
            from backend.strategy.ols import residual_from_frozen_fit
            resid_now = residual_from_frozen_fit(dependent_symbol, prices_now, position["betas"], position["intercept"])
            exit_dec = decide_exit(resid_now, position["direction"], position["mean"], position["std"], z_close, z_stop_loss)
            if exit_dec.should_exit:
                pnl = simulate_pnl(position["entry_residual"], resid_now, position["direction"], num_legs, transaction_cost_rate)
                trades.append(BacktestTrade(
                    entry_idx=position["entry_idx"], exit_idx=i,
                    entry_time=position["entry_time"], close_time=price_df.index[i],
                    direction=position["direction"], entry_z=position["entry_z"], close_z=exit_dec.z,
                    close_reason=exit_dec.reason, pnl=pnl,
                ))
                position = None
            i += 1
            continue

        window = price_df.iloc[i - window_size:i]
        price_matrix = {s: window[s].to_numpy() for s in symbols}
        try:
            fit = fit_ols(dependent_symbol, price_matrix)
        except Exception:
            i += 1
            continue

        stat = test_stationarity(fit.residual, adf_alpha, kpss_alpha)
        if not stat.passed or fit.resid_std == 0:
            i += 1
            continue

        resid_now = fit.residual[-1]
        entry_dec = decide_entry(resid_now, fit.resid_mean, fit.resid_std, z_entry)
        if entry_dec.should_enter:
            position = {
                "direction": entry_dec.direction,
                "betas": fit.betas,
                "intercept": fit.intercept,
                "mean": fit.resid_mean,
                "std": fit.resid_std,
                "entry_idx": i,
                "entry_time": price_df.index[i],
                "entry_residual": resid_now,
                "entry_z": entry_dec.z,
            }
        i += 1

    closed = [{"pnl": t.pnl, "entry_time": t.entry_time, "close_time": t.close_time} for t in trades]
    perf = compute_group_performance(closed)
    return BacktestResult(trades=trades, performance=perf)
