"""
Beta-aware leg sizing for raw-price OLS residuals.

    y = a + sum(beta_j * x_j) + residual

Long residual (expect residual to rise): buy qty_y of y, and for each j
hold -beta_j * qty_y of x_j  (i.e. sell if beta>0, buy if beta<0).

    qty_y  = trade_notional / P_y
    qty_xj = |beta_j| * qty_y

No log-price linearization — betas are used directly as share ratios.
"""
from __future__ import annotations


def leg_orders(
    dependent_symbol: str,
    direction: str,
    betas: dict,
    prices: dict,
    trade_notional: float,
) -> list[dict]:
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
        same_side = b < 0
        side = dep_side if same_side else ("sell" if dep_side == "buy" else "buy")
        qty = abs(b) * dep_qty
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
    out = []
    for leg in legs_entry or []:
        sym = leg["symbol"]
        out.append(
            {
                "symbol": sym,
                "side": "sell" if leg["side"] == "buy" else "buy",
                "qty": float(leg["qty"]),
                "price": float(close_prices[sym]),
            }
        )
    return out
