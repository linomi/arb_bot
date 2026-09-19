from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models import BotState
from backend.schemas import BotModeUpdate
from backend.engine.bot_engine import bot_engine, get_or_create_bot_state

router = APIRouter(prefix="/api/bot", tags=["bot"])


@router.get("/state")
def get_state(db: Session = Depends(get_db)):
    state = get_or_create_bot_state(db)
    return {
        "is_running": state.is_running,
        "trading_mode": state.trading_mode,
        "last_error": bot_engine._last_error,
    }


@router.post("/start")
def start_bot(db: Session = Depends(get_db)):
    """
    Flips BotState.is_running on. The background loop (already alive since
    server start) will pick this up on its next tick. Does NOT depend on
    this HTTP connection or any open browser tab staying open.
    """
    state = get_or_create_bot_state(db)
    state.is_running = True
    db.commit()
    return {"is_running": True}


@router.post("/stop")
def stop_bot(db: Session = Depends(get_db)):
    state = get_or_create_bot_state(db)
    state.is_running = False
    db.commit()
    return {"is_running": False}


@router.post("/mode")
def set_mode(body: BotModeUpdate, db: Session = Depends(get_db)):
    if body.trading_mode not in ("paper", "live"):
        raise HTTPException(400, "trading_mode must be 'paper' or 'live'")
    state = get_or_create_bot_state(db)
    state.trading_mode = body.trading_mode
    db.commit()
    return {"trading_mode": state.trading_mode}
