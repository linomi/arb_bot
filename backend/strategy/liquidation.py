"""Modeled liquidation prices and the liquidation-proximity check.

One implementation shared by the live engine (fallback when the exchange reports no liquidation
price), the paper engine and the backtester, so all three apply the same guard.

Model (isolated margin, per leg; margin = leg notional / leverage):
    long : liq = entry * (1 - 1/L + mmr)        (price falling)
    short: liq = entry * (1 + 1/L - mmr)        (price rising)
``mmr`` is the maintenance margin rate (``maintenance_margin_rate`` in the backbone config).
Real exchange values depend on risk tiers and fees; for live trading the exchange-reported
liquidation price is used first. Cross margin cannot be modeled per leg, so it is approximated
as isolated here.

The guard fires when ``|mark - liq| / mark <= threshold`` (or the mark is already past liq)."""
from __future__ import annotations

DEFAULT_MMR = 0.005


def liq_price(entry_price: float, side: str, leverage: float, mmr: float = DEFAULT_MMR) -> float:
    lev = max(1.0, float(leverage or 1.0))
    p = float(entry_price)
    if str(side).lower() in ("buy", "long"):
        return max(0.0, p * (1.0 - 1.0 / lev + float(mmr)))
    return p * (1.0 + 1.0 / lev - float(mmr))


def legs_from_betas(dependent_symbol: str, direction: str, betas: dict, prices: dict) -> list[dict]:
    """Per-leg side and entry price for a trade, using the same side rule as live sizing."""
    dep_side = "buy" if direction == "long_residual" else "sell"
    legs = [{"symbol": dependent_symbol, "side": dep_side, "price": float(prices[dependent_symbol])}]
    for sym, beta in (betas or {}).items():
        if sym == dependent_symbol or sym not in prices:
            continue
        same_side = float(beta) < 0
        side = dep_side if same_side else ("sell" if dep_side == "buy" else "buy")
        legs.append({"symbol": sym, "side": side, "price": float(prices[sym])})
    return legs


def _entry_px(leg: dict):
    for k in ("fill_price", "price"):
        v = leg.get(k)
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
    return None


def nearest_liquidation(legs: list[dict], marks: dict, leverage: float, mmr: float = DEFAULT_MMR) -> dict | None:
    """The leg closest to its modeled liquidation price: {symbol, mark, liq, dist}.
    ``dist`` is a fraction of the mark price and is <= 0 once the mark is past liquidation."""
    worst = None
    for leg in legs or []:
        sym = leg.get("symbol")
        entry = _entry_px(leg)
        mark = marks.get(sym)
        side = leg.get("side")
        if entry is None or mark is None or not side:
            continue
        mark = float(mark)
        if mark <= 0:
            continue
        liq = liq_price(entry, side, leverage, mmr)
        is_long = str(side).lower() in ("buy", "long")
        past = (mark <= liq) if is_long else (mark >= liq)
        dist = -abs(mark - liq) / mark if past else abs(mark - liq) / mark
        if worst is None or dist < worst["dist"]:
            worst = {"symbol": sym, "mark": mark, "liq": liq, "dist": dist}
    return worst


def near_liquidation(legs: list[dict], marks: dict, leverage: float, threshold: float,
                     mmr: float = DEFAULT_MMR) -> dict | None:
    """Return the offending leg's info if any leg is within ``threshold`` of liquidation."""
    if not threshold or float(threshold) <= 0:
        return None
    w = nearest_liquidation(legs, marks, leverage, mmr)
    if w is not None and w["dist"] <= float(threshold):
        return w
    return None


def max_safe_leverage(threshold: float, mmr: float = DEFAULT_MMR) -> float:
    """Leverage below which a fresh position starts outside the guard band.
    At entry mark == entry price, so the cushion is 1/L - mmr; it must exceed ``threshold``."""
    t = float(threshold or 0)
    if t <= 0:
        return float("inf")
    return 1.0 / (t + float(mmr))
