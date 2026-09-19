"""Manual group creation endpoint (user-selected symbols)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import Group
from backend.schemas import ManualGroupCreate

router = APIRouter(prefix="/api/groups", tags=["groups"])


def _group_to_dict(g: Group) -> dict:
    return {
        "id": g.id, "name": g.name, "symbols": g.symbols,
        "dependent_symbol": g.dependent_symbol, "source": g.source,
        "sector": g.sector, "status": g.status,
        "backtest_metrics": g.backtest_metrics,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


@router.post("/manual")
def create_manual_group(body: ManualGroupCreate, db: Session = Depends(get_db)):
    """Create a group from a user-selected set of symbols (no backtest required)."""
    symbols = [s.strip().upper() for s in (body.symbols or []) if s and str(s).strip()]
    seen = set()
    uniq = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    if len(uniq) < 2:
        raise HTTPException(400, "Select at least 2 distinct symbols")
    dependent = (body.dependent_symbol or uniq[0]).strip().upper()
    if dependent not in uniq:
        raise HTTPException(400, f"dependent_symbol {dependent} must be one of the selected symbols")
    name = (body.name or "").strip() or f"manual-{'-'.join(uniq)}"[:80]
    g = Group(
        name=name,
        symbols=uniq,
        dependent_symbol=dependent,
        source="manual",
        sector=None,
        status="active" if body.activate else "candidate",
        params_snapshot=None,
        backtest_metrics=None,
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return _group_to_dict(g)
