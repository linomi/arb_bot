"""
Initialization approaches: random_init / sector_init.

Candidate-group backtests run in a process pool so OLS + ADF/KPSS
use multiple CPU cores.

Ranking (after backtest):
  1. Keep only groups with total_pnl > 0 (and, from the Initialization tab / run_init, at least
     min_trades in-sample trades and a positive held-out PnL when require_oos_positive)
  2. Score = t-statistic of the mean trade PnL (see _score_group), so the trade count counts
"""
from __future__ import annotations

import itertools
import math
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

import numpy as np
import pandas as pd

from backend.sectors import group_symbols_by_sector
from backend.engine.backtester import rounding_cfg_from_params, backtest_group
from backend.strategy.signal import signal_cfg_from_params
from backend.strategy.account import leverage_for

# Trades of typical size pooled into the score's spread estimate (see _score_group).
_PRIOR_TRADES = 4

_WORKER_PRICE_DF: pd.DataFrame | None = None
_WORKER_PARAMS: dict | None = None


def _worker_init(price_df: pd.DataFrame, backbone_params: dict) -> None:
    global _WORKER_PRICE_DF, _WORKER_PARAMS
    _WORKER_PRICE_DF = price_df
    _WORKER_PARAMS = backbone_params


def _worker_eval(job: tuple) -> dict | None:
    return _evaluate_job(job, _WORKER_PRICE_DF, _WORKER_PARAMS)


def evaluate_group(price_df: pd.DataFrame, symbols: list[str], dependent: str,
                   backbone_params: dict, source: str = "manual", progress_cb=None) -> dict | None:
    """Backtest ONE user-chosen group with the user-chosen dependent symbol (no dependent search),
    using exactly the same in-sample/out-of-sample procedure as the automatic init."""
    ordered = [dependent] + [s for s in symbols if s != dependent]
    params = {**backbone_params, "try_all_dependents": False}
    res = _evaluate_job((ordered, source, None), price_df, params, progress_cb)
    if res is not None:
        res["symbols"] = list(symbols)
    return res


def _evaluate_job(job: tuple, price_df_full, backbone_params, progress_cb=None) -> dict | None:
    symbols, source, sector = job
    if price_df_full is None or backbone_params is None:
        return None

    cols = [s for s in symbols if s in price_df_full.columns]
    if len(cols) != len(symbols):
        return None
    sub_df = price_df_full[list(symbols)].dropna()
    if len(sub_df) < int(backbone_params["window_size"]) + 5:
        return None

    # The residual (and therefore the whole strategy) depends on which symbol is
    # regressed on the others; try each and keep the best-scoring choice instead
    # of always taking the alphabetically-first symbol.
    candidates = list(symbols) if backbone_params.get("try_all_dependents", True) else [symbols[0]]
    best = None
    n = len(sub_df)
    split = max(int(n * 0.7), int(backbone_params["window_size"]) + 10)
    is_df = sub_df.iloc[:split]
    oos_df = sub_df.iloc[split - int(backbone_params["window_size"]):]  # overlap window for continuity
    is_share = len(is_df) / max(1, len(is_df) + max(0, len(oos_df) - int(backbone_params["window_size"])))
    W = int(backbone_params["window_size"])

    def _bt(df, dependent, cb):
        return backtest_group(
            progress_cb=cb,
            price_df=df,
            dependent_symbol=dependent,
            window_size=W,
            adf_alpha=float(backbone_params["adf_alpha"]),
            kpss_alpha=float(backbone_params["kpss_alpha"]),
            z_entry=float(backbone_params["z_entry"]),
            z_close=float(backbone_params["z_close"]),
            z_stop_loss=float(backbone_params["z_stop_loss"]),
            transaction_cost_rate=float(backbone_params["transaction_fee_rate"]),
            target_profit_rate=float(backbone_params.get("target_profit_rate", 0.0) or 0.0),
            max_entry_scale=backbone_params.get("max_entry_scale"),
            trade_notional=float(backbone_params.get("trade_notional", 100.0) or 100.0),
            max_holding_bars=backbone_params.get("max_holding_bars"),
            stationarity_method=backbone_params.get("stationarity_method", "engle_granger"),
            half_life_max_fraction=float(backbone_params.get("half_life_max_fraction", 1.0/3.0) or 0),
            leverage=leverage_for(None, backbone_params, dependent),
            liquidation_proximity=float(backbone_params.get("liquidation_proximity_fraction", 0) or 0),
            maintenance_margin_rate=float(backbone_params.get("maintenance_margin_rate", 0.005) or 0.005),
            rounding=rounding_cfg_from_params(backbone_params, list(symbols)),
            signal=signal_cfg_from_params(backbone_params),
        )

    # In-sample backtest for every dependent; the held-out part is only needed for the one that is kept.
    is_part = is_share / len(candidates)
    for k, dependent in enumerate(candidates):
        try:
            bt = _bt(is_df, dependent,
                     (lambda f, k=k: progress_cb((k + f) * is_part)) if progress_cb else None)
        except Exception:
            continue
        sc = _score_group(bt.trades)
        if best is None or sc > best[2]:
            best = (dependent, bt, sc)
    if best is None:
        return None
    dependent, bt, score = best
    if len(bt.trades) < int(backbone_params.get("min_trades", 0) or 0):
        return None
    oos_perf = None
    oos_score = None
    if len(oos_df) > W + 5:
        try:
            bt_oos = _bt(oos_df, dependent,
                         (lambda f: progress_cb(is_share + f * (1 - is_share))) if progress_cb else None)
            oos_perf = bt_oos.performance.as_dict()
            oos_score = _score_group(bt_oos.trades)
        except Exception:
            pass
    if backbone_params.get("require_oos_positive") and oos_perf is not None:
        if float(oos_perf.get("total_pnl") or 0) <= 0:
            return None

    return {
        "symbols": list(symbols),
        "dependent_symbol": dependent,
        "source": source,
        "sector": sector,
        "backtest_metrics": bt.performance.as_dict(),
        "contract_rounding": bt.rounding,
        "score": score,
        "oos_metrics": oos_perf,
        "oos_score": oos_score,
    }


def _align_price_df(ohlc_by_symbol: dict[str, dict]) -> pd.DataFrame:
    """Outer-join every symbol's closes on time. Rows are NOT dropped here: each group keeps the bars where
    ITS symbols all have a price (``_evaluate_job``), so one recently listed coin cannot shorten every other
    group's history."""
    series = {}
    for sym, ohlc in ohlc_by_symbol.items():
        if not ohlc.get("t"):
            continue
        idx = pd.to_datetime(ohlc["t"], unit="s")
        series[sym] = pd.Series(ohlc["c"], index=idx)
    return pd.DataFrame(series).dropna(how="all").sort_index()


def _score_group(trades) -> float:
    """
    Rank score (higher = better): the t-statistic of the mean trade PnL, i.e. how many standard errors the
    edge is above zero. It grows like sqrt(trade count) for a given edge, so a group that made money on many
    trades outranks one that got lucky on two, and it is scale-free (raw-price PnL of a QNT basket and an
    ARB basket compare directly).

    The spread is pooled with ``_PRIOR_TRADES`` trades of the group's typical |PnL|, so 1-3 winners cannot
    claim a near-zero spread: 1 winner scores 1.0, 2 equal winners 1.6, 3 -> 2.1, 10 -> 5.7, while 7 equal
    wins and 3 equal losses out of 10 score about 1.3. No trades -> -1e9.
    """
    pnl = np.array([float(getattr(t, "pnl", t)) for t in trades], dtype=float)
    n = len(pnl)
    if n == 0:
        return -1e9
    mean = float(pnl.mean())
    typical = float(np.mean(np.abs(pnl)))
    if typical <= 0:
        return 0.0
    var = float(np.var(pnl, ddof=1)) if n > 1 else 0.0
    spread = math.sqrt(((n - 1) * var + _PRIOR_TRADES * typical ** 2) / (n - 1 + _PRIOR_TRADES))
    return mean / spread * math.sqrt(n)


def _select_top(ranked: list[dict], keep_top_n: int) -> list[dict]:
    """Positive PnL first (already scored); drop non-positive for the kept set."""
    positive = [
        r for r in ranked
        if float((r.get("backtest_metrics") or {}).get("total_pnl") or 0) > 0
    ]
    # Already sorted by score descending in _backtest_candidates
    return positive[: max(0, int(keep_top_n))]


def _default_workers() -> int:
    # Physical cores when known: hyper-threads add little to this CPU-bound work, and on a 4-core / 8-thread
    # PC 7 workers were ~15% slower than 4.
    try:
        import psutil
        physical = psutil.cpu_count(logical=False)
    except ImportError:
        physical = None
    if physical:
        return max(1, min(physical, 12))
    n = os.cpu_count() or 4
    if n <= 2:
        return n
    return max(2, min(n - 1, 12))


def _backtest_candidates(
    candidate_symbol_sets: list[tuple[str, ...]],
    ohlc_by_symbol: dict[str, dict],
    backbone_params: dict,
    source: str,
    sector_lookup: dict[str, str] | None = None,
    progress_cb=None,
    max_workers: int | None = None,
) -> list[dict]:
    price_df_full = _align_price_df(ohlc_by_symbol)
    total = len(candidate_symbol_sets)
    if total == 0:
        return []

    workers = max_workers or _default_workers()
    workers = max(1, min(workers, total))

    jobs: list[tuple[Any, ...]] = []
    for symbols in candidate_symbol_sets:
        sector = sector_lookup.get(symbols[0]) if sector_lookup else None
        jobs.append((symbols, source, sector))

    results: list[dict] = []
    done = 0

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(price_df_full, backbone_params),
    ) as pool:
        futs = {pool.submit(_worker_eval, job): job[0] for job in jobs}
        for fut in as_completed(futs):
            done += 1
            symbols = futs[fut]
            if progress_cb is not None:
                try:
                    progress_cb(done, total, symbols)
                except Exception:
                    pass
            try:
                row = fut.result()
            except Exception:
                row = None
            if row is not None:
                results.append(row)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def random_init(
    ohlc_by_symbol: dict[str, dict],
    min_group_size: int,
    max_group_size: int,
    num_candidate_groups: int,
    keep_top_n: int,
    backbone_params: dict,
    rng_seed: int | None = None,
    progress_cb=None,
    max_workers: int | None = None,
) -> list[dict]:
    symbols = list(ohlc_by_symbol.keys())
    rng = random.Random(rng_seed)
    seen = set()
    candidates: list[tuple[str, ...]] = []
    attempts = 0
    max_attempts = num_candidate_groups * 20
    while len(candidates) < num_candidate_groups and attempts < max_attempts:
        attempts += 1
        size = rng.randint(min_group_size, min(max_group_size, len(symbols)))
        if size < 2:
            continue
        group = tuple(sorted(rng.sample(symbols, size)))
        if group in seen:
            continue
        seen.add(group)
        candidates.append(group)

    ranked = _backtest_candidates(
        candidates, ohlc_by_symbol, backbone_params, source="random",
        progress_cb=progress_cb, max_workers=max_workers,
    )
    return _select_top(ranked, keep_top_n)


def sector_init(
    ohlc_by_symbol: dict[str, dict],
    min_group_size: int,
    max_group_size: int,
    keep_top_n: int,
    backbone_params: dict,
    max_combos_per_sector: int = 200,
    rng_seed: int | None = None,
    progress_cb=None,
    max_workers: int | None = None,
) -> list[dict]:
    symbols = list(ohlc_by_symbol.keys())
    buckets = group_symbols_by_sector(symbols)
    sector_lookup = {sym: sec for sec, syms in buckets.items() for sym in syms}
    rng = random.Random(rng_seed)

    candidates: list[tuple[str, ...]] = []
    for sector, syms in buckets.items():
        if len(syms) < 2:
            continue
        combos = []
        for size in range(min_group_size, min(max_group_size, len(syms)) + 1):
            combos.extend(itertools.combinations(sorted(syms), size))
        if len(combos) > max_combos_per_sector:
            combos = rng.sample(combos, max_combos_per_sector)
        candidates.extend(combos)

    ranked = _backtest_candidates(
        candidates, ohlc_by_symbol, backbone_params, source="sector",
        sector_lookup=sector_lookup, progress_cb=progress_cb, max_workers=max_workers,
    )
    return _select_top(ranked, keep_top_n)
