"""Shared account maths: trade gross notional, margin, and the paper-mode wallet.

Used by the API (trade table, equity curve) and by the bot engine (paper balance
check), so both always agree on what a position costs.
"""
from __future__ import annotations

from backend.models import Group, Trade
from backend.strategy.pnl import legs_gross_notional, fills_pnl_and_fee

USDT_SWAP_SUFFIX = "/USDT:USDT"


def is_xt_group(exchange: str | None, dependent_symbol: str | None = None) -> bool:
    """A USDT-settled perpetual symbol is an XT group even if the stored exchange column
    says otherwise (older rows were saved as nobitex)."""
    return (exchange or "nobitex").lower() == "xt" or str(dependent_symbol or "").endswith(USDT_SWAP_SUFFIX)


def leverage_for(exchange: str | None, backbone: dict, dependent_symbol: str | None = None) -> float:
    """Leverage that turns gross notional into margin (Nobitex orders use 1x)."""
    if is_xt_group(exchange, dependent_symbol):
        try:
            return max(1.0, float(backbone.get("xt_leverage", 1) or 1))
        except (TypeError, ValueError):
            return 1.0
    return 1.0


def trade_gross(t: Trade) -> float | None:
    """Total entry notional across all legs (exposure), from fills when known."""
    legs = getattr(t, "legs_entry", None)
    if legs:
        g = legs_gross_notional(legs)
        if g > 0:
            return float(g)
    tn = getattr(t, "trade_notional", None)
    if tn is None:
        return None
    if getattr(t, "notional_basis", None) == "gross":
        return float(tn)
    # Legacy row without stored legs: trade_notional was the dependent leg only.
    try:
        from backend.strategy.sizing import leg_orders
        fit = getattr(t, "ols_fit", None)
        dep = t.group.dependent_symbol
        legs = leg_orders(dep, t.direction, (fit.betas if fit else {}) or {}, t.entry_prices or {},
                          float(tn), min_order_value=0.0, basis="dependent")
        return float(legs_gross_notional(legs))
    except Exception:
        return float(tn)


def paper_pnl(t: Trade) -> float:
    for v in (t.pnl, getattr(t, "model_pnl", None)):
        if v is not None:
            return float(v)
    return 0.0


def paper_free_balance(db, backbone: dict) -> float | None:
    """Free USDT in the simulated paper wallet shared by all XT groups:

        paper_start_balance + realized PnL of closed paper trades - margin of open paper trades

    Returns None when the check is off (paper_start_balance <= 0)."""
    try:
        start = float(backbone.get("paper_start_balance", 0) or 0)
    except (TypeError, ValueError):
        return None
    if start <= 0:
        return None
    free = start
    rows = db.query(Trade, Group).join(Group, Group.id == Trade.group_id).filter(Trade.mode == "paper").all()
    for t, g in rows:
        if not is_xt_group(getattr(g, "exchange", None), g.dependent_symbol):
            continue  # IRT (Nobitex) groups are not in this USDT wallet
        if t.status == "closed":
            free += paper_pnl(t)
        elif t.status == "open":
            gross = trade_gross(t)
            if gross:
                free -= gross / leverage_for(getattr(g, "exchange", None), backbone, g.dependent_symbol)
    return free


def migrate_paper_pnl_net_of_fees(db, fee_rate: float) -> int:
    """One-time, idempotent fix for paper trades closed before fees were subtracted from the
    fill-based PnL. A row is converted only when its stored ``pnl`` equals the gross fills figure
    (so model-based or already-adjusted rows are left alone). The fee that was subtracted is stored in
    ``realized_fee`` (which also marks the row as done); the original number is ``pnl + realized_fee``.
    ``fee_rate`` is the backbone fee currently configured. Returns the number of trades converted."""
    rows = (
        db.query(Trade)
        .filter(Trade.mode == "paper", Trade.status == "closed", Trade.realized_fee.is_(None))
        .all()
    )
    n = 0
    for t in rows:
        if t.pnl is None or not t.legs_entry or not t.legs_close:
            continue
        fp = fills_pnl_and_fee(t.legs_entry, t.legs_close, fee_rate)
        if fp is None:
            continue
        gross, fee = fp
        if abs(float(t.pnl) - gross) > max(1e-6, 1e-6 * abs(gross)):
            continue
        t.pnl = gross - fee
        if t.realized_pnl is not None:
            t.realized_pnl = gross - fee
        t.realized_fee = fee
        n += 1
    if n:
        db.commit()
    return n
