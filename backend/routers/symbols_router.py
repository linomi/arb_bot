"""Liquid symbols listing for the manual group UI."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db import get_db
from backend import config_service
from backend.exchange import factory

router = APIRouter(prefix="/api/init", tags=["init"])


@router.get("/symbols")
async def list_liquid_symbols(top_n: int | None = None, db: Session = Depends(get_db)):
    """Return top liquid market symbols for the manual group picker."""
    init_cfg = config_service.get_section(db, "init")
    n = int(top_n) if top_n else int(init_cfg.get("top_n_liquid_symbols", 50))
    n = max(5, min(n, 200))
    quote = init_cfg.get("quote_currency", "IRT")
    md = factory.build_market_data_client(db)
    try:
        symbols = await md.get_liquid_symbols(top_n=n, quote=quote)
    finally:
        await md.aclose()
    return {"symbols": symbols, "quote": quote, "top_n": n}
