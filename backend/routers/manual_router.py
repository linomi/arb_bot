"""Manual group creation endpoint (user-selected symbols)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import Group
from backend.schemas import ManualGroupCreate
from backend.group_names import generate_group_name
from backend.strategy.sizing import assert_same_quote, assert_same_exchange
from backend.bot_state_service import get_or_create_bot_state

router = APIRouter(prefix="/api/groups", tags=["groups"])


def _group_to_dict(g: Group) -> dict:
    return {
        "id": g.id, "name": g.name, "symbols": g.symbols,
        "dependent_symbol": g.dependent_symbol, "source": g.source,
        "sector": g.sector, "status": g.status,
        "exchange": getattr(g, "exchange", None) or "nobitex",
        "backtest_metrics": g.backtest_metrics,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


@router.post("/manual")
def create_manual_group(body: ManualGroupCreate, db: Session = Depends(get_db)):
    symbols = [s.strip() for s in (body.symbols or []) if s and str(s).strip()]
    # Preserve unified ccxt form (BTC/USDT:USDT); only upper-case non-slash forms
    normalized = []
    for s in symbols:
        if "/" in s or ":" in s:
            normalized.append(s)  # keep unified form as-is (ccxt is case-sensitive-ish)
        else:
            normalized.append(s.upper())
    seen = set()
    uniq = []
    for s in normalized:
        key = s.upper()
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    if len(uniq) < 2:
        raise HTTPException(400, "Select at least 2 distinct symbols")

    state = get_or_create_bot_state(db)
    exchange = (body.exchange or getattr(state, "exchange", None) or "nobitex").strip().lower()

    dependent = body.dependent_symbol or uniq[0]
    if dependent.upper() not in {u.upper() for u in uniq}:
        # try exact match
        if dependent not in uniq:
            raise HTTPException(
                400,
                f"dependent_symbol {dependent} must be one of the selected symbols",
            )

    try:
        assert_same_exchange(uniq, exchange, dependent)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    used = {n for (n,) in db.query(Group.name).all()}
    name = (body.name or "").strip() or generate_group_name(used=used)
    g = Group(
        name=name,
        symbols=uniq,
        dependent_symbol=dependent,
        source="manual",
        sector=None,
        status="active" if body.activate else "candidate",
        exchange=exchange,
        params_snapshot=None,
        backtest_metrics=None,
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return _group_to_dict(g)
