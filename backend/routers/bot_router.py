from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.schemas import BotModeUpdate, BotExchangeUpdate
from backend.engine.bot_engine import bot_engine
from backend.bot_state_service import get_or_create_bot_state
from backend.exchange.factory import credentials_configured, SUPPORTED_EXCHANGES

router = APIRouter(prefix="/api/bot", tags=["bot"])


@router.get("/state")
def get_state(db: Session = Depends(get_db)):
    state = get_or_create_bot_state(db)
    exchange = getattr(state, "exchange", None) or "nobitex"
    return {
        "is_running": state.is_running,
        "trading_mode": state.trading_mode,
        "exchange": exchange,
        "last_error": bot_engine._last_error,
        "group_errors": dict(getattr(bot_engine, "_group_errors", {}) or {}),
        "pause_until": getattr(bot_engine, "_pause_until", None),
        "credentials_configured": credentials_configured(db, exchange),
        "orphan_positions": list(getattr(bot_engine, "_orphan_positions", []) or []),
        "note": "is_running=False only blocks new entries; open positions are still managed",
    }


@router.post("/start")
def start_bot(db: Session = Depends(get_db)):
    state = get_or_create_bot_state(db)
    exchange = getattr(state, "exchange", None) or "nobitex"
    if state.trading_mode == "live" and not credentials_configured(db, exchange):
        raise HTTPException(
            400,
            f"Cannot start in live mode without saved API credentials for {exchange}.",
        )
    state.is_running = True
    db.commit()
    return {"is_running": True}


@router.post("/stop")
def stop_bot(db: Session = Depends(get_db)):
    """Pause new entries only — exit/stop-loss still runs for open trades."""
    state = get_or_create_bot_state(db)
    state.is_running = False
    db.commit()
    return {
        "is_running": False,
        "message": "New entries paused; open positions still managed",
    }


@router.post("/mode")
def set_mode(body: BotModeUpdate, db: Session = Depends(get_db)):
    if body.trading_mode not in ("paper", "live"):
        raise HTTPException(400, "trading_mode must be 'paper' or 'live'")
    state = get_or_create_bot_state(db)
    exchange = getattr(state, "exchange", None) or "nobitex"
    if body.trading_mode == "live" and not credentials_configured(db, exchange):
        raise HTTPException(
            400,
            f"Save API credentials for {exchange} under Settings → Credentials before switching to Live.",
        )
    state.trading_mode = body.trading_mode
    db.commit()
    return {"trading_mode": state.trading_mode, "exchange": exchange}


@router.post("/exchange")
def set_exchange(body: BotExchangeUpdate, db: Session = Depends(get_db)):
    """Select the single active exchange for live/paper market data + trading."""
    ex = (body.exchange or "").strip().lower()
    if ex not in SUPPORTED_EXCHANGES:
        raise HTTPException(400, f"exchange must be one of {SUPPORTED_EXCHANGES}")
    state = get_or_create_bot_state(db)
    state.exchange = ex
    db.commit()
    return {"exchange": state.exchange, "trading_mode": state.trading_mode}
