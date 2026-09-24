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
    total = 0.0
    for l in legs or []:
        q = l.get("filled_qty")
        if q is None:
            q = l.get("qty") or 0
        p = l.get("price") or 0
        total += abs(float(q)) * float(p)
    return float(total)


def gross_per_unit_y(
    prices: dict,
    dependent_symbol: str,
    betas: dict,
) -> float:
    """
    Gross notional per 1 unit of dependent (qty_y = 1):
        G = P_y + Σ |β_j| · P_xj
    """
    py = float(prices.get(dependent_symbol) or 0)
    g = abs(py)
    for sym, beta in (betas or {}).items():
        if sym == dependent_symbol:
            continue
        px = float(prices.get(sym) or 0)
        g += abs(float(beta)) * abs(px)
    return float(g)


def entry_target_check(
    z_now: float,
    sigma: float,
    z_close: float,
    z_stop: float,
    gross_per_unit_y: float,
    cost_rate: float,
    target_rate: float,
    stop_margin: float = 0.75,
) -> tuple[bool, dict]:
    """
    Accept entry only if a perfect close at |z| = z_close would leave
    net_frac >= target_rate of G, and the required z_min is not too close
    to the stop (z_min <= z_stop - stop_margin).

    If target_rate <= 0 the gate is disabled (always accept).
    """
    details: dict = {
        "sigma_rel": None,
        "edge_frac": None,
        "cost_frac": None,
        "net_frac": None,
        "z_min": None,
        "disabled": False,
        "reject_reason": None,
    }

    tr = float(target_rate)
    if tr <= 0:
        details["disabled"] = True
        return True, details

    G = float(gross_per_unit_y)
    sig = float(sigma)
    if G <= 0 or sig <= 0 or sig != sig:
        details["reject_reason"] = "invalid_G_or_sigma"
        return False, details

    sigma_rel = sig / G
    if sigma_rel <= 0 or sigma_rel != sigma_rel:
        details["reject_reason"] = "invalid_sigma_rel"
        return False, details

    cost_frac = 2.0 * float(cost_rate)
    edge_frac = sigma_rel * (abs(float(z_now)) - float(z_close))
    net_frac = edge_frac - cost_frac
    z_min = float(z_close) + (cost_frac + tr) / sigma_rel

    details.update({
        "sigma_rel": float(sigma_rel),
        "edge_frac": float(edge_frac),
        "cost_frac": float(cost_frac),
        "net_frac": float(net_frac),
        "z_min": float(z_min),
    })

    if net_frac < tr:
        details["reject_reason"] = "net_below_target"
        return False, details

    if z_min > float(z_stop) - float(stop_margin):
        details["reject_reason"] = "z_min_near_stop"
        return False, details

    return True, details
