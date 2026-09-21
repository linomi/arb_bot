"""
Bar-by-bar replay of the backbone logic over historical OHLC closes.

PnL model (raw-price OLS): cash ≈ qty_y * Δresidual − costs, with
qty_y = 1 share of the dependent for ranking (unit share).
"""
from dataclasses import dataclass
import pandas as pd

from backend.strategy.ols import fit_ols, residual_from_frozen_fit
from backend.strategy.stats_tests import test_stationarity
from backend.strategy.zscore import decide_entry, decide_exit
from backend.strategy.metrics import compute_group_performance, GroupPerformance
from backend.strategy.pnl import residual_cash_pnl


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


def simulate_pnl(
    entry_residual: float,
    exit_residual: float,
    direction: str,
    num_legs: int,
    transaction_cost_rate: float,
    qty_dependent: float = 1.0,
    gross_notional: float | None = None,
) -> float:
    if gross_notional is None:
        gross_notional = float(num_legs)
    return residual_cash_pnl(
        entry_residual, exit_residual, direction,
        qty_dependent, transaction_cost_rate, gross_notional,
    )


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
    symbols = list(price_df.columns)
    n = len(price_df)
    trades: list[BacktestTrade] = []
    position = None

    i = window_size
    while i < n:
        prices_now = price_df.iloc[i].to_dict()

        if position is not None:
            resid_now = residual_from_frozen_fit(
                dependent_symbol, prices_now, position["betas"], position["intercept"],
            )
            exit_dec = decide_exit(
                resid_now, position["direction"], position["mean"], position["std"],
                z_close, z_stop_loss,
            )
            if exit_dec.should_exit:
                py = float(position["entry_prices"].get(dependent_symbol) or prices_now[dependent_symbol])
                gross = abs(py) * (1.0 + sum(abs(float(b)) for b in position["betas"].values()))
                pnl = residual_cash_pnl(
                    position["entry_residual"], resid_now, position["direction"],
                    qty_dependent=1.0,
                    cost_rate=transaction_cost_rate,
                    gross_notional=gross,
                )
                trades.append(BacktestTrade(
                    entry_idx=position["entry_idx"], exit_idx=i,
                    entry_time=position["entry_time"], close_time=price_df.index[i],
                    direction=position["direction"], entry_z=position["entry_z"],
                    close_z=exit_dec.z, close_reason=exit_dec.reason, pnl=pnl,
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

        resid_now = float(fit.residual[-1])
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
                "entry_prices": prices_now,
            }
        i += 1

    closed = [{"pnl": t.pnl, "entry_time": t.entry_time, "close_time": t.close_time} for t in trades]
    perf = compute_group_performance(closed)
    return BacktestResult(trades=trades, performance=perf)
