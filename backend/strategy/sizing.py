"""
Beta-aware leg sizing for raw-price OLS residuals.

    y = a + sum(beta_j * x_j) + residual

qty_y  = trade_notional / P_y
qty_xj = |beta_j| * qty_y

Nobitex margin has a hard ~50_000 IRT minimum order value. We scale all legs
up so every IRT-quoted leg notional is at least that floor.
"""
from __future__ import annotations

# Nobitex practical minimum order value for IRT markets (Rial).
MIN_ORDER_VALUE_IRT = 50_000.0


def leg_orders(
    dependent_symbol: str,
    direction: str,
    betas: dict,
    prices: dict,
    trade_notional: float,
    min_order_value_irt: float = MIN_ORDER_VALUE_IRT,
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

    # Scale up so every IRT leg meets the exchange minimum order value.
    scale = 1.0
    for leg in legs:
        if _is_irt(leg["symbol"]):
            leg_notional = float(leg["qty"]) * float(leg["price"])
            if leg_notional > 0 and leg_notional < min_order_value_irt:
                scale = max(scale, min_order_value_irt / leg_notional)
    if scale > 1.0:
        for leg in legs:
            leg["qty"] = float(leg["qty"]) * scale
            leg["scaled_for_min"] = True
            leg["scale"] = scale

    return legs


def _is_irt(symbol: str) -> bool:
    s = (symbol or "").upper()
    return s.endswith("IRT") or s.endswith("RLS")


def close_legs_from_entry(legs_entry: list[dict], close_prices: dict) -> list[dict]:
    out = []
    for leg in legs_entry or []:
        sym = leg["symbol"]
        out.append(
            {
                "symbol": sym,
                "side": "sell" if leg["side"] == "buy" else "buy",
                "qty": float(leg["qty"]),
                "price": float(close_prices.get(sym, leg.get("price") or 0)),
                "position_id": leg.get("position_id"),
                "order_id": leg.get("order_id"),
            }
        )
    return out


def min_leg_notional_irt(legs: list[dict]) -> float:
    vals = []
    for leg in legs or []:
        if _is_irt(leg.get("symbol", "")):
            vals.append(float(leg["qty"]) * float(leg.get("price") or 0))
    return min(vals) if vals else 0.0
