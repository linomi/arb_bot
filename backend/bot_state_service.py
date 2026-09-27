from backend.models import BotState


def get_or_create_bot_state(db) -> BotState:
    state = db.query(BotState).first()
    if state is None:
        state = BotState(is_running=False, trading_mode="paper", exchange="nobitex")
        db.add(state)
        db.commit()
        db.refresh(state)
    # Backfill exchange on very old rows if column exists but is NULL
    if getattr(state, "exchange", None) is None:
        state.exchange = "nobitex"
        db.commit()
        db.refresh(state)
    return state
