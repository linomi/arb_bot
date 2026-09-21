"""
Initialization approaches: random_init / sector_init.

Candidate-group backtests run in a thread pool (CPU-bound OLS + ADF/KPSS)
so many groups are evaluated in parallel.
"""
from __future__ import annotations

import itertools
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from backend.sectors import group_symbols_by_sector
from backend.engine.backtester import backtest_group


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
    if perf.trade_count == 0:
        return -1e9
    return float(perf.total_pnl) * 1000.0 + float(perf.sharpe_ratio)


def _default_workers() -> int:
    # Leave one core for the event loop / OS; cap to avoid thrashing on small VPS.
    n = os.cpu_count() or 4
    return max(2, min(n, 12))


def _eval_one_group(
    symbols: tuple[str, ...],
    price_df_full: pd.DataFrame,
    backbone_params: dict,
    source: str,
    sector_lookup: dict[str, str] | None,
) -> dict | None:
    cols = [s for s in symbols if s in price_df_full.columns]
    if len(cols) != len(symbols):
        return None
    sub_df = price_df_full[list(symbols)].dropna()
    if len(sub_df) < int(backbone_params["window_size"]) + 5:
        return None

    dependent = symbols[0]
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
        )
    except Exception:
        return None

    return {
        "symbols": list(symbols),
        "dependent_symbol": dependent,
        "source": source,
        "sector": sector_lookup.get(symbols[0]) if sector_lookup else None,
        "backtest_metrics": bt.performance.as_dict(),
        "score": _score_group(bt.performance),
    }


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
    results: list[dict] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                _eval_one_group,
                symbols,
                price_df_full,
                backbone_params,
                source,
                sector_lookup,
            ): symbols
            for symbols in candidate_symbol_sets
        }
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
    return ranked[:keep_top_n]


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
    return ranked[:keep_top_n]
