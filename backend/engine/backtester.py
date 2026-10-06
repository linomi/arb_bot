"""
Bar-by-bar replay of the backbone logic over historical OHLC closes.

PnL model (raw-price OLS): cash ≈ qty_y * Δresidual − costs, with
qty_y = 1 share of the dependent for ranking (unit share).
Gross notional uses G = P_y + Σ|β|·P_x (same as live legs_gross_notional).
"""
from dataclasses import dataclass
import pandas as pd

from backend.strategy.ols import fit_ols, residual_from_frozen_fit
from backend.strategy.stats_tests import test_stationarity
from backend.strategy.zscore import decide_entry, decide_exit, ExitDecision
from backend.strategy.half_life import half_life_ok
from backend.strategy.metrics import compute_group_performance, GroupPerformance
from backend.strategy.pnl import residual_cash_pnl, gross_per_unit_y, entry_target_check


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
    target_profit_rate: float = 0.0,
    max_entry_scale: float | None = None,
    trade_notional: float = 100.0,
    max_holding_bars: int | None = None,
    stationarity_method: str = "engle_granger",
    mark_open_at_end: bool = True,
    half_life_max_fraction: float = 1.0 / 3.0,
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
            time_stop = (
                not exit_dec.should_exit
                and max_holding_bars
                and (i - position["entry_idx"]) >= int(max_holding_bars)
            )
            if time_stop:
                exit_dec = ExitDecision(True, "time_stop", exit_dec.z)
            if exit_dec.should_exit:
                gross = gross_per_unit_y(
                    position["entry_prices"], dependent_symbol, position["betas"],
                )
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

        if fit.resid_std == 0:
            i += 1
            continue

        resid_now = float(fit.residual[-1])
        entry_dec = decide_entry(
            resid_now, fit.resid_mean, fit.resid_std, z_entry, z_stop=z_stop_loss,
        )
        if entry_dec.should_enter:
            # Signal first, expensive stationarity test only when a signal exists
            # (identical result, ~50x fewer ADF/EG runs).
            stat = test_stationarity(
                fit.residual, adf_alpha, kpss_alpha,
                y=price_matrix[dependent_symbol],
                x_cols=[price_matrix[s] for s in symbols if s != dependent_symbol],
                method=stationarity_method,
            )
            if not stat.passed:
                i += 1
                continue
            if half_life_max_fraction and half_life_max_fraction > 0:
                ok_hl, _hl = half_life_ok(
                    fit.residual, window_size, max_fraction=float(half_life_max_fraction),
                )
                if not ok_hl:
                    i += 1
                    continue
            G_unit = gross_per_unit_y(prices_now, dependent_symbol, fit.betas)
            ok, _det = entry_target_check(
                z_now=entry_dec.z,
                sigma=fit.resid_std,
                z_close=z_close,
                z_stop=z_stop_loss,
                gross_per_unit_y=G_unit,
                cost_rate=transaction_cost_rate,
                target_rate=float(target_profit_rate or 0.0),
            )
            if not ok:
                i += 1
                continue
            if max_entry_scale is not None and float(max_entry_scale) > 0:
                try:
                    from backend.strategy.sizing import leg_orders, ExcessiveScalingError
                    leg_orders(
                        dependent_symbol,
                        entry_dec.direction,
                        fit.betas,
                        prices_now,
                        float(trade_notional),
                        max_scale=float(max_entry_scale),
                    )
                except ExcessiveScalingError:
                    i += 1
                    continue
                except Exception:
                    pass
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

    if position is not None and mark_open_at_end and n > 0:
        last_prices = price_df.iloc[-1].to_dict()
        resid_last = residual_from_frozen_fit(
            dependent_symbol, last_prices, position["betas"], position["intercept"],
        )
        gross = gross_per_unit_y(position["entry_prices"], dependent_symbol, position["betas"])
        pnl = residual_cash_pnl(
            position["entry_residual"], resid_last, position["direction"],
            qty_dependent=1.0, cost_rate=transaction_cost_rate, gross_notional=gross,
        )
        trades.append(BacktestTrade(
            entry_idx=position["entry_idx"], exit_idx=n - 1,
            entry_time=position["entry_time"], close_time=price_df.index[-1],
            direction=position["direction"], entry_z=position["entry_z"],
            close_z=(resid_last - position["mean"]) / position["std"] if position["std"] else 0.0,
            close_reason="open_mtm", pnl=pnl,
        ))

    closed = [{"pnl": t.pnl, "entry_time": t.entry_time, "close_time": t.close_time} for t in trades]
    perf = compute_group_performance(closed)
    return BacktestResult(trades=trades, performance=perf)
