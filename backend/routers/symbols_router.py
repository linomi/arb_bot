"""Liquid symbols listing for the manual group UI."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.db import get_db
from backend import config_service
from backend.exchange import factory
from backend.bot_state_service import get_or_create_bot_state

router = APIRouter(prefix="/api/init", tags=["init"])

# Valid quote per exchange for v1
_QUOTE_FOR_EXCHANGE = {
    "nobitex": "IRT",
    "xt": "USDT",
}


@router.get("/symbols")
async def list_liquid_symbols(
    top_n: int | None = None,
    exchange: str | None = Query(None),
    quote: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Return top liquid market symbols for the manual group picker."""
    state = get_or_create_bot_state(db)
    ex = (exchange or getattr(state, "exchange", None) or "nobitex").strip().lower()
    if ex not in _QUOTE_FOR_EXCHANGE:
        raise HTTPException(400, f"unsupported exchange: {ex}")

    init_cfg = config_service.get_section(db, "init")
    n = int(top_n) if top_n else int(init_cfg.get("top_n_liquid_symbols", 50))
    n = max(5, min(n, 200))

    default_quote = _QUOTE_FOR_EXCHANGE[ex]
    q = (quote or init_cfg.get("quote_currency") or default_quote).upper()
    # Reject invalid quote×exchange early
    if q != default_quote:
        raise HTTPException(
            400,
            f"quote_currency={q} is not valid for exchange={ex} (expected {default_quote})",
        )

    # Temporarily ensure factory sees the requested exchange
    prev = getattr(state, "exchange", "nobitex")
    state.exchange = ex
    db.commit()
    try:
        md = factory.build_market_data_client(db)
        try:
            symbols = await md.get_liquid_symbols(top_n=n, quote=q)
        finally:
            await md.aclose()
    finally:
        state.exchange = prev
        db.commit()

    return {"symbols": symbols, "quote": q, "top_n": n, "exchange": ex}
