"""Realized cash PnL from actual entry/close leg fills."""
from __future__ import annotations


def realized_cash_pnl(
    legs_entry: list[dict],
    legs_close: list[dict],
    fee_rate: float,
) -> tuple[float, float]:
    """
    Returns (realized_pnl, realized_fee) in quote currency.

    For each symbol:
      signed_qty = +qty if entry buy, -qty if entry sell
      leg_pnl = signed_qty * (close_price - entry_price)
      fee = fee_rate * qty * (entry_price + close_price)  # both sides
    """
    if not legs_entry:
        return 0.0, 0.0
    close_by = {l["symbol"]: l for l in (legs_close or [])}
    pnl = 0.0
    fee = 0.0
    fr = float(fee_rate or 0.0)
    for entry_leg in legs_entry:
        sym = entry_leg["symbol"]
        if sym not in close_by:
            raise KeyError(f"missing close leg for {sym}")
        close_leg = close_by[sym]
        qty = float(entry_leg["qty"])
        pe = float(entry_leg["price"])
        pc = float(close_leg["price"])
        sign = 1.0 if entry_leg["side"] == "buy" else -1.0
        pnl += sign * qty * (pc - pe)
        fee += fr * qty * (pe + pc)
    return float(pnl - fee), float(fee)
