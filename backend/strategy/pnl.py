"""
Cash PnL from raw-price residual moves.

With raw OLS  y = a + Σ β_j x_j + R  and share hedge qty_xj = β_j * qty_y,
portfolio cash PnL before fees ≈ qty_y * ΔR.
"""
from __future__ import annotations


def residual_cash_pnl(
    entry_residual: float,
    exit_residual: float,
    direction: str,
    qty_dependent: float,
    cost_rate: float,
    gross_notional: float,
) -> float:
    if direction == "short_residual":
        delta = float(entry_residual) - float(exit_residual)
    else:
        delta = float(exit_residual) - float(entry_residual)
    gross = float(qty_dependent) * delta
    cost = float(cost_rate) * 2.0 * float(gross_notional)
    return float(gross - cost)


def legs_gross_notional(legs: list[dict]) -> float:
    return float(sum(abs(float(l["qty"])) * float(l["price"]) for l in (legs or [])))
