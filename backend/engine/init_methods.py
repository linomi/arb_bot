"""
Initialization approaches: random_init / sector_init.

Candidate-group backtests run in a process pool so OLS + ADF/KPSS
use multiple CPU cores.

Ranking (after backtest):
  1. Keep only groups with total_pnl > 0
  2. Score balances low max drawdown vs sample size (few trades with
     tiny DD are not preferred over steadier series with more trades)
"""
from __future__ import annotations

import itertools
import math
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

import pandas as pd

from backend.sectors import group_symbols_by_sector
from backend.engine.backtester import backtest_group

# Soft sample-size anchor: at n ≈ this, trade-count confidence ≈ 2/3.
_TRADE_CONF_HALF = 20

_WORKER_PRICE_DF: pd.DataFrame | None = None
_WORKER_PARAMS: dict | None = None


def _worker_init(price_df: pd.DataFrame, backbone_params: dict) -> None:
    global _WORKER_PRICE_DF, _WORKER_PARAMS
    _WORKER_PRICE_DF = price_df
    _WORKER_PARAMS = backbone_params


def _worker_eval(job: tuple) -> dict | None:
    symbols, source, sector = job
    price_df_full = _WORKER_PRICE_DF
    backbone_params = _WORKER_PARAMS
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
    for dependent in candidates:
        try:
            bt = backtest_group(
                price_df=sub_df,
                dependent_symbol=dependent,
                window_size=int(backbone_params["window_size"]),
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
            )
        except Exception:
            continue
        sc = _score_group(bt.performance)
        if best is None or sc > best[2]:
            best = (dependent, bt, sc)
    if best is None:
        return None
    dependent, bt, _sc = best

    return {
        "symbols": list(symbols),
        "dependent_symbol": dependent,
        "source": source,
        "sector": sector,
        "backtest_metrics": bt.performance.as_dict(),
        "score": _score_group(bt.performance),
    }


def _align_price_df(ohlc_by_symbol: dict[str, dict]) -> pd.DataFrame:
    series = {}
    for sym, ohlc in ohlc_by_symbol.items():
        if not ohlc.get("t"):
            continue
        idx = pd.to_datetime(ohlc["t"], unit="s")
        series[sym] = pd.Series(ohlc["c"], index=idx)
    df = pd.DataFrame(series).dropna(how="any").sort_index()
    return df


def _score_group(perf) -> float:
    """
    Balanced rank score (higher = better).

    Design:
      - Non-positive PnL or zero trades → buried at the bottom
      - Primary quality: PnL / effective_drawdown (Calmar-style)
      - effective_drawdown floors tiny DDs that only look good because
        there were 1–2 lucky trades
      - trade_conf ∈ (0, 1) grows with trade_count so low-n series
        cannot dominate solely on a near-zero DD
      - Small secondary terms for win_rate and sharpe
    """
    n = int(getattr(perf, "trade_count", 0) or 0)
    pnl = float(getattr(perf, "total_pnl", 0.0) or 0.0)
    dd = float(getattr(perf, "max_drawdown", 0.0) or 0.0)
    avg = float(getattr(perf, "avg_pnl", 0.0) or 0.0)
    wr = float(getattr(perf, "win_rate", 0.0) or 0.0)
    sharpe = float(getattr(perf, "sharpe_ratio", 0.0) or 0.0)

    if n < 1 or pnl <= 0:
        # Still differentiate slightly so sort is stable among rejects
        return -1e9 + min(pnl, 0.0) - 0.01 * dd

    # Confidence in the sample: n=6 → ~0.5, n=18 → ~0.75, asymptote 1
    trade_conf = n / (n + _TRADE_CONF_HALF)

    # Floor DD so a single small loss (or none) cannot inflate Calmar.
    # Use a fraction of mean |trade| × sqrt(n) as a statistical noise floor.
    avg_abs = abs(avg) if avg else abs(pnl) / max(n, 1)
    noise_floor = avg_abs * 0.35 * math.sqrt(max(n, 1))
    # Also absolute tiny floor for near-zero scale series
    dd_eff = max(dd, noise_floor, 1e-9)

    calmar = pnl / dd_eff

    # Prefer lower raw DD among similar Calmar (secondary tie-break via inverse)
    dd_bonus = 1.0 / (1.0 + dd / max(avg_abs, 1e-9))

    score = (
        calmar * trade_conf
        + 0.25 * trade_conf * max(wr, 0.0)
        + 0.10 * trade_conf * max(sharpe, 0.0)
        + 0.05 * trade_conf * dd_bonus
    )
    return float(score)


def _select_top(ranked: list[dict], keep_top_n: int) -> list[dict]:
    """Positive PnL first (already scored); drop non-positive for the kept set."""
    positive = [
        r for r in ranked
        if float((r.get("backtest_metrics") or {}).get("total_pnl") or 0) > 0
    ]
    # Already sorted by score descending in _backtest_candidates
    return positive[: max(0, int(keep_top_n))]


def _default_workers() -> int:
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
