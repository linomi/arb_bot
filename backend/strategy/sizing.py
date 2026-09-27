"""
Beta-aware leg sizing for raw-price OLS residuals.

    y = a + sum(beta_j * x_j) + residual

qty_y  = trade_notional / P_y
qty_xj = |beta_j| * qty_y

Negligible independent legs (contrib << dependent notional) are dropped
instead of scaling the whole basket up to meet the exchange min order size.
Remaining legs are then scaled so each meets min_order_value.
"""
from __future__ import annotations

import logging

log = logging.getLogger("sizing")

# Single source of truth for Nobitex IRT minimum order value (Rial).
# XT uses per-market limits from load_markets instead.
MIN_ORDER_VALUE_IRT = 50_000.0

# Drop independent leg if its notional is below this fraction of dependent notional.
DEFAULT_MIN_WEIGHT_FRACTION = 0.02


class ExcessiveScalingError(ValueError):
    """Raised when leg_orders would inflate the basket beyond max_scale."""

    def __init__(self, scale: float, symbol: str | None = None, max_scale: float | None = None):
        self.scale = scale
        self.symbol = symbol
        self.max_scale = max_scale
        msg = f"scale {scale:.3f}"
        if symbol:
            msg += f" required for {symbol}"
        if max_scale is not None:
            msg += f" exceeds max_scale={max_scale}"
        super().__init__(msg)


def leg_orders(
    dependent_symbol: str,
    direction: str,
    betas: dict,
    prices: dict,
    trade_notional: float,
    min_order_value: float | None = None,
    min_order_value_irt: float | None = None,  # backward-compat alias
    min_weight_fraction: float = DEFAULT_MIN_WEIGHT_FRACTION,
    max_scale: float | None = None,
) -> list[dict]:
    if direction not in ("long_residual", "short_residual"):
        raise ValueError(f"unknown direction: {direction}")
    notional = float(trade_notional)
    if notional <= 0:
        raise ValueError("trade_notional must be positive")

    # Resolve minimum: explicit min_order_value > legacy min_order_value_irt > default IRT
    if min_order_value is not None:
        min_val = float(min_order_value)
    elif min_order_value_irt is not None:
        min_val = float(min_order_value_irt)
    else:
        min_val = MIN_ORDER_VALUE_IRT

    py = float(prices[dependent_symbol])
    if py <= 0:
        raise ValueError(f"invalid price for {dependent_symbol}: {py}")

    dep_side = "buy" if direction == "long_residual" else "sell"
    dep_qty = notional / py
    legs: list[dict] = [
        {
            "symbol": dependent_symbol,
            "side": dep_side,
            "qty": float(dep_qty),
            "price": py,
            "is_dependent": True,
        }
    ]

    dropped: list[dict] = []
    for sym, beta in (betas or {}).items():
        b = float(beta)
        px = float(prices[sym])
        if px <= 0:
            raise ValueError(f"invalid price for {sym}: {px}")

        # Approximate leg notional at current dep qty: |β| * qty_y * P_x
        leg_notional = abs(b) * dep_qty * px
        weight = leg_notional / notional if notional > 0 else 0.0
        if weight < float(min_weight_fraction):
            dropped.append({
                "symbol": str(sym),
                "beta": b,
                "weight": weight,
                "leg_notional": leg_notional,
                "reason": f"weight {weight:.4f} < min_weight_fraction {min_weight_fraction}",
            })
            continue

        same_side = b < 0
        side = dep_side if same_side else ("sell" if dep_side == "buy" else "buy")
        qty = abs(b) * dep_qty
        legs.append({
            "symbol": str(sym),
            "side": side,
            "qty": float(qty),
            "price": px,
            "is_dependent": False,
            "beta": b,
        })

    if dropped:
        log.info(
            "dropped negligible legs (min_weight=%.3f): %s",
            min_weight_fraction,
            [(d["symbol"], round(d["weight"], 5)) for d in dropped],
        )

    # Scale remaining legs to meet exchange minimum — never inflate for dropped legs.
    scale = 1.0
    scale_symbol = None
    for leg in legs:
        leg_notional = float(leg["qty"]) * float(leg["price"])
        if leg_notional > 0 and leg_notional < min_val:
            needed = min_val / leg_notional
            if needed > scale:
                scale = needed
                scale_symbol = leg["symbol"]

    if max_scale is not None and scale > float(max_scale):
        raise ExcessiveScalingError(scale, symbol=scale_symbol, max_scale=float(max_scale))

    if scale > 1.0:
        for leg in legs:
            leg["qty"] = float(leg["qty"]) * scale
            leg["scaled_for_min"] = True
            leg["scale"] = scale

    # Attach drop list on dependent leg metadata for audit trail
    legs[0]["dropped_legs"] = dropped
    return legs


def _is_irt(symbol: str) -> bool:
    s = (symbol or "").upper()
    return s.endswith("IRT") or s.endswith("RLS")


def quote_suffix(symbol: str) -> str:
    s = (symbol or "").upper()
    # Unified ccxt swap form BTC/USDT:USDT
    if ":USDT" in s or s.endswith(":USDT"):
        return "USDT"
    if s.endswith("USDT") or "/USDT" in s:
        return "USDT"
    if s.endswith("IRT") or s.endswith("RLS"):
        return "IRT"
    if len(s) > 3:
        return s[-3:]
    return s


def assert_same_quote(symbols: list[str], dependent: str | None = None) -> str:
    """Raise ValueError if symbols mix quote currencies. Returns common quote."""
    if not symbols:
        raise ValueError("no symbols")
    quotes = {quote_suffix(s) for s in symbols}
    if dependent:
        quotes.add(quote_suffix(dependent))
    if len(quotes) != 1:
        raise ValueError(
            f"mixed quote currencies in group: {sorted(quotes)} "
            f"(symbols={symbols}, dependent={dependent})"
        )
    return next(iter(quotes))


def assert_same_exchange(
    symbols: list[str],
    exchange: str,
    dependent: str | None = None,
) -> str:
    """
    Ensure symbols are consistent with the group's exchange.
    Nobitex groups must be IRT-quoted; XT groups must be USDT swap form.
    A group cannot mix a Nobitex IRT symbol with an XT USDT perpetual.
    """
    quote = assert_same_quote(symbols, dependent)
    ex = (exchange or "nobitex").lower()
    if ex == "nobitex" and quote != "IRT":
        raise ValueError(
            f"nobitex groups require IRT quote, got {quote} "
            f"(symbols={symbols}, dependent={dependent})"
        )
    if ex == "xt" and quote != "USDT":
        raise ValueError(
            f"xt groups require USDT settle, got {quote} "
            f"(symbols={symbols}, dependent={dependent})"
        )
    return quote


def close_legs_from_entry(legs_entry: list[dict], close_prices: dict) -> list[dict]:
    out = []
    for leg in legs_entry or []:
        if leg.get("already_closed") or leg.get("failed"):
            # still include for bookkeeping with zero qty close attempt skip
            pass
        sym = leg["symbol"]
        qty = float(leg.get("filled_qty") if leg.get("filled_qty") is not None else leg["qty"])
        out.append({
            "symbol": sym,
            "side": "sell" if leg["side"] == "buy" else "buy",
            "qty": qty,
            "price": float(close_prices.get(sym, leg.get("price") or 0)),
            "position_id": leg.get("position_id"),
            "order_id": leg.get("order_id"),
        })
    return out


def legs_gross_notional(legs: list[dict]) -> float:
    total = 0.0
    for leg in legs or []:
        q = float(leg.get("filled_qty") if leg.get("filled_qty") is not None else leg.get("qty") or 0)
        p = float(leg.get("fill_price") if leg.get("fill_price") is not None else leg.get("price") or 0)
        total += abs(q * p)
    return total


def min_leg_notional_irt(legs: list[dict]) -> float:
    vals = []
    for leg in legs or []:
        if _is_irt(leg.get("symbol", "")):
            vals.append(float(leg["qty"]) * float(leg.get("price") or 0))
    return min(vals) if vals else 0.0


def total_required_collateral(legs: list[dict], leverage: float = 1.0) -> float:
    """Approx collateral at given leverage (sum of leg notionals / leverage)."""
    lev = max(float(leverage), 1e-9)
    return legs_gross_notional(legs) / lev
