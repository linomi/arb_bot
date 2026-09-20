"""
Beta-aware leg sizing for residual trades.

Log-price OLS:  log(P_y) = a + sum(beta_j * log(P_xj)) + residual
Local linear hedge: size independent j at |beta_j| * trade_notional (quote),
side same as dependent iff beta_j < 0.
"""
from __future__ import annotations


def leg_orders(
    dependent_symbol: str,
    direction: str,
    betas: dict,
    prices: dict,
    trade_notional: float,
) -> list[dict]:
    """
    direction: "long_residual" | "short_residual"
    betas: {independent_symbol: beta_j} from the OLS fit for this trade
    prices: {symbol: price}
    Returns list of {"symbol","side","qty","price"} — dependent leg first.
    """
    if direction not in ("long_residual", "short_residual"):
        raise ValueError(f"unknown direction: {direction}")
    notional = float(trade_notional)
    if notional <= 0:
        raise ValueError("trade_notional must be positive")

    py = float(prices[dependent_symbol])
    if py <= 0:
        raise ValueError(f"invalid price for {dependent_symbol}: {py}")

    dep_side = "buy" if direction == "long_residual" else "sell"
    dep_qty = notional / py
    legs = [
        {
            "symbol": dependent_symbol,
            "side": dep_side,
            "qty": float(dep_qty),
            "price": py,
        }
    ]

    for sym, beta in (betas or {}).items():
        b = float(beta)
        px = float(prices[sym])
        if px <= 0:
            raise ValueError(f"invalid price for {sym}: {px}")
        # negative beta => same side as dependent (inverse relationship)
        same_side = b < 0
        if same_side:
            side = dep_side
        else:
            side = "sell" if dep_side == "buy" else "buy"
        qty = notional * abs(b) / px
        legs.append(
            {
                "symbol": str(sym),
                "side": side,
                "qty": float(qty),
                "price": px,
            }
        )
    return legs


def close_legs_from_entry(legs_entry: list[dict], close_prices: dict) -> list[dict]:
    """
    Mirror entry legs: same qty, flipped side, mark at close prices.
    Keeps hedge ratio locked to entry (no mid-trade resize).
    """
    out = []
    for leg in legs_entry or []:
        sym = leg["symbol"]
        px = float(close_prices[sym])
        out.append(
            {
                "symbol": sym,
                "side": "sell" if leg["side"] == "buy" else "buy",
                "qty": float(leg["qty"]),
                "price": px,
            }
        )
    return out
