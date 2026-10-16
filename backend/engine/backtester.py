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
from backend.strategy.liquidation import legs_from_betas, near_liquidation, DEFAULT_MMR
from backend.strategy.sizing import (
    ContractRoundingError, drift_gate, leg_orders, legs_gross_notional, quote_suffix, rounding_info,
)


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
    # filled only when whole-contract rounding is simulated (per unit of the dependent, like ``pnl``)
    pnl_ideal: float | None = None
    hedge_drift: float | None = None


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    performance: GroupPerformance
    rounding: dict | None = None


def rounding_cfg_from_params(params: dict, symbols: list[str]) -> dict | None:
    """Whole-contract rounding settings for a backtest, or None when it does not apply (not an XT USDT
    basket, rounding off, or contract sizes not cached yet)."""
    mode = str(params.get("contract_rounding", "joint") or "off")
    if mode == "off" or not symbols or any(quote_suffix(s) != "USDT" for s in symbols):
        return None
    from backend.exchange import contract_sizes
    cs = contract_sizes.lookup_many(symbols)
    if len(cs) < len(symbols):
        return None  # unknown contract size for some symbol: do not guess
    return {
        "contract_sizes": cs, "mode": mode,
        "max_size_dev": float(params.get("contract_max_size_dev", 0.35) or 0.35),
        "max_drift": float(params.get("contract_max_drift", 0.15) or 0.0),
        "move": float(params.get("contract_drift_move", 0.01) or 0.0),
        "noise_ratio": float(params.get("contract_noise_ratio_max", 0.5) or 0.0),
    }


def _legs_pnl(legs: list[dict], p0: dict, p1: dict, cost_rate: float) -> float:
    """Cash PnL of an actual basket between two price snapshots, net of round-trip costs."""
    pnl = 0.0
    for l in legs:
        sgn = 1.0 if l["side"] == "buy" else -1.0
        pnl += sgn * float(l["qty"]) * (float(p1[l["symbol"]]) - float(p0[l["symbol"]]))
    return pnl - cost_rate * 2.0 * sum(abs(float(l["qty"]) * float(p0[l["symbol"]])) for l in legs)


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
    leverage: float = 1.0,
    liquidation_proximity: float = 0.0,
    maintenance_margin_rate: float = DEFAULT_MMR,
    progress_cb=None,
    rounding: dict | None = None,
) -> BacktestResult:
    """``liquidation_proximity`` > 0 turns on the same guard as the live/paper engine: a trade is
    closed with reason ``risk_exit`` when any leg's mark is within that fraction of its modeled
    liquidation price (see strategy/liquidation.py), and entries that would start inside the band
    are skipped. ``leverage`` is the exchange leverage (1 for Nobitex)."""
    symbols = list(price_df.columns)
    n = len(price_df)
    trades: list[BacktestTrade] = []
    position = None
    _min_notional = 5.0 if quote_suffix(dependent_symbol) == "USDT" else None
    rstats = {"enabled": bool(rounding), "entries": 0, "skipped_rounding": 0, "skipped_drift": 0,
              "pnl_ideal_usdt": 0.0, "pnl_ideal_same_size_usdt": 0.0, "pnl_rounded_usdt": 0.0, "drifts": []} if rounding else None

    def _finish(pos, resid_exit, exit_prices, base_pnl):
        """(pnl, pnl_ideal, drift) for a closing trade; rounded basket PnL replaces the model PnL."""
        if not rounding or not pos.get("rounded_legs"):
            return base_pnl, None, None
        qy = float(pos["ideal_qty_y"])
        pr = _legs_pnl(pos["rounded_legs"], pos["entry_prices"], exit_prices, transaction_cost_rate)
        pi = _legs_pnl(pos["ideal_legs"], pos["entry_prices"], exit_prices, transaction_cost_rate)
        rstats["pnl_rounded_usdt"] += pr
        rstats["pnl_ideal_usdt"] += pi
        # the ideal basket scaled to the rounded basket's total size: what is left is the pure hedge-drift effect
        rstats["pnl_ideal_same_size_usdt"] += pi * float(pos.get("size_ratio") or 1.0)
        return pr / qy, pi / qy, pos.get("hedge_drift")

    i = window_size
    _step = max(1, (n - window_size) // 50)  # ~50 progress ticks
    while i < n:
        if progress_cb is not None and (i - window_size) % _step == 0:
            try:
                progress_cb((i - window_size) / max(1, n - window_size))
            except Exception:
                pass
        prices_now = price_df.iloc[i].to_dict()

        if position is not None:
            resid_now = residual_from_frozen_fit(
                dependent_symbol, prices_now, position["betas"], position["intercept"],
            )
            exit_dec = decide_exit(
                resid_now, position["direction"], position["mean"], position["std"],
                z_close, z_stop_loss,
            )
            if not exit_dec.should_exit and liquidation_proximity and liquidation_proximity > 0:
                if near_liquidation(position["legs"], prices_now, leverage,
                                    liquidation_proximity, maintenance_margin_rate):
                    exit_dec = ExitDecision(True, "risk_exit", exit_dec.z)
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
                pnl, pnl_ideal, drift = _finish(position, resid_now, prices_now, pnl)
                trades.append(BacktestTrade(
                    entry_idx=position["entry_idx"], exit_idx=i,
                    entry_time=position["entry_time"], close_time=price_df.index[i],
                    direction=position["direction"], entry_z=position["entry_z"],
                    close_z=exit_dec.z, close_reason=exit_dec.reason, pnl=pnl,
                    pnl_ideal=pnl_ideal, hedge_drift=drift,
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
                    from backend.strategy.sizing import ExcessiveScalingError
                    # USDT baskets use a small exchange minimum; the IRT default (50,000) would
                    # make every USDT entry look like a huge scale-up and skip it.
                    _min = _min_notional
                    leg_orders(
                        dependent_symbol,
                        entry_dec.direction,
                        fit.betas,
                        prices_now,
                        float(trade_notional),
                        min_order_value=_min,
                        max_scale=float(max_entry_scale),
                    )
                except ExcessiveScalingError:
                    i += 1
                    continue
                except Exception:
                    pass
            rounded_legs = ideal_legs = None
            drift_val = None
            if rounding:
                try:
                    ideal_legs = leg_orders(dependent_symbol, entry_dec.direction, fit.betas, prices_now,
                                            float(trade_notional), min_order_value=_min_notional)
                    rounded_legs = leg_orders(
                        dependent_symbol, entry_dec.direction, fit.betas, prices_now, float(trade_notional),
                        min_order_value=_min_notional, contract_sizes=rounding["contract_sizes"],
                        contract_mode=rounding["mode"], contract_max_size_dev=rounding["max_size_dev"],
                    )
                except ContractRoundingError:
                    rstats["skipped_rounding"] += 1
                    i += 1
                    continue
                except Exception:
                    rounded_legs = ideal_legs = None
                if rounded_legs:
                    info = rounding_info(rounded_legs) or {}
                    gross_r = legs_gross_notional(rounded_legs)
                    sigma_rel = fit.resid_std / G_unit if G_unit > 0 else 0.0
                    exp_net = (sigma_rel * (abs(entry_dec.z) - z_close) - 2.0 * transaction_cost_rate) * gross_r
                    ok_d, _dd = drift_gate(
                        info, expected_net_profit=exp_net, max_drift=rounding["max_drift"],
                        move_estimate=rounding["move"], max_noise_ratio=rounding["noise_ratio"],
                    )
                    if not ok_d:
                        rstats["skipped_drift"] += 1
                        i += 1
                        continue
                    drift_val = float(info.get("hedge_drift") or 0.0)
            entry_legs = legs_from_betas(dependent_symbol, entry_dec.direction, fit.betas, prices_now)
            if liquidation_proximity and liquidation_proximity > 0 and near_liquidation(
                entry_legs, prices_now, leverage, liquidation_proximity, maintenance_margin_rate,
            ):
                i += 1  # would open inside the guard band: the engine skips these too
                continue
            if rounded_legs:
                rstats["entries"] += 1
                rstats["drifts"].append(drift_val or 0.0)
            position = {
                "legs": entry_legs,
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
                "rounded_legs": rounded_legs,
                "ideal_legs": ideal_legs,
                "ideal_qty_y": (next((l["ideal_qty"] if "ideal_qty" in l else l["qty"]
                                      for l in (rounded_legs or []) if l["symbol"] == dependent_symbol), None)
                                if rounded_legs else None),
                "hedge_drift": drift_val,
                "size_ratio": float((rounding_info(rounded_legs) or {}).get("size_ratio") or 1.0) if rounded_legs else 1.0,
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
        pnl, pnl_ideal, drift = _finish(position, resid_last, last_prices, pnl)
        trades.append(BacktestTrade(
            entry_idx=position["entry_idx"], exit_idx=n - 1,
            entry_time=position["entry_time"], close_time=price_df.index[-1],
            direction=position["direction"], entry_z=position["entry_z"],
            close_z=(resid_last - position["mean"]) / position["std"] if position["std"] else 0.0,
            close_reason="open_mtm", pnl=pnl, pnl_ideal=pnl_ideal, hedge_drift=drift,
        ))

    closed = [{"pnl": t.pnl, "entry_time": t.entry_time, "close_time": t.close_time, "direction": t.direction} for t in trades]
    perf = compute_group_performance(closed)
    rounding_report = None
    if rstats is not None:
        import numpy as np
        d = rstats.pop("drifts")
        rounding_report = {
            **rstats,
            "trades": len(trades),
            "hedge_drift_mean": float(np.mean(d)) if d else None,
            "hedge_drift_p90": float(np.percentile(d, 90)) if d else None,
            "hedge_drift_max": float(np.max(d)) if d else None,
            "pnl_lost_to_rounding_usdt": rstats["pnl_ideal_usdt"] - rstats["pnl_rounded_usdt"],
            "pnl_lost_to_size_usdt": rstats["pnl_ideal_usdt"] - rstats["pnl_ideal_same_size_usdt"],
            "pnl_lost_to_drift_usdt": rstats["pnl_ideal_same_size_usdt"] - rstats["pnl_rounded_usdt"],
        }
    return BacktestResult(trades=trades, performance=perf, rounding=rounding_report)
