"""Debug / inspection endpoints (funding rates, etc.)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.bot_state_service import get_or_create_bot_state
from backend.exchange import factory

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/funding-rates")
async def funding_rates(
    symbol: str = Query(..., description="Unified swap symbol e.g. BTC/USDT:USDT"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Recent funding rate history for an XT USDT-M symbol.
    Use this to tune backbone.funding_rate_estimate from real data.
    """
    state = get_or_create_bot_state(db)
    exchange = getattr(state, "exchange", None) or "nobitex"
    if exchange != "xt":
        raise HTTPException(
            400,
            "funding-rates is only available when BotState.exchange=xt",
        )
    md = factory.build_market_data_client(db)
    try:
        if not hasattr(md, "fetch_funding_rate_history"):
            raise HTTPException(400, "active client does not support funding history")
        rows = await md.fetch_funding_rate_history(symbol, limit=limit)
        return {"symbol": symbol, "limit": limit, "rates": rows}
    finally:
        await md.aclose()
