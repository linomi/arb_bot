"""
The two initialization approaches from the spec:

  1) random_init   -- top-N liquid symbols -> random groups (min/max size) ->
                       backtest each -> keep top-N performers.
  2) sector_init    -- symbols bucketed by sector (backend/sectors.py) ->
                       combinations WITHIN each sector (min/max size) ->
                       same backtest+prune step -> keep survivors.

Both return a list of dicts describing groups ready to be persisted as
`Group` rows with status="candidate" (or "active" if auto-activated).
"""
import itertools
import random
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
    """Ranking score for pruning: total PnL primary, Sharpe as tiebreak.
    Groups with zero trades are ranked last.
    """
    if perf.trade_count == 0:
        return -1e9
    return float(perf.total_pnl) * 1000.0 + float(perf.sharpe_ratio)


def _backtest_candidates(
    candidate_symbol_sets: list[tuple[str, ...]],
    ohlc_by_symbol: dict[str, dict],
    backbone_params: dict,
    source: str,
    sector_lookup: dict[str, str] | None = None,
    progress_cb=None,
) -> list[dict]:
    price_df_full = _align_price_df(ohlc_by_symbol)
    results = []
    total = len(candidate_symbol_sets)
    for idx, symbols in enumerate(candidate_symbol_sets):
        if progress_cb is not None:
            progress_cb(idx + 1, total, symbols)
        cols = [s for s in symbols if s in price_df_full.columns]
        if len(cols) != len(symbols):
            continue
        sub_df = price_df_full[list(symbols)].dropna()
        if len(sub_df) < backbone_params["window_size"] + 5:
            continue

        dependent = symbols[0]  # first symbol arbitrarily chosen as dependent
        bt = backtest_group(
            price_df=sub_df,
            dependent_symbol=dependent,
            window_size=backbone_params["window_size"],
            adf_alpha=backbone_params["adf_alpha"],
            kpss_alpha=backbone_params["kpss_alpha"],
            z_entry=backbone_params["z_entry"],
            z_close=backbone_params["z_close"],
            z_stop_loss=backbone_params["z_stop_loss"],
            transaction_cost_rate=backbone_params["transaction_fee_rate"],
        )
        results.append({
            "symbols": list(symbols),
            "dependent_symbol": dependent,
            "source": source,
            "sector": sector_lookup.get(symbols[0]) if sector_lookup else None,
            "backtest_metrics": bt.performance.as_dict(),
            "score": _score_group(bt.performance),
        })
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
        candidates, ohlc_by_symbol, backbone_params, source="random", progress_cb=progress_cb,
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
        sector_lookup=sector_lookup, progress_cb=progress_cb,
    )
    return ranked[:keep_top_n]
