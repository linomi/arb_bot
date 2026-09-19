from backend.models import BotState


def get_or_create_bot_state(db) -> BotState:
    state = db.query(BotState).first()
    if state is None:
        state = BotState(is_running=False, trading_mode="paper")
        db.add(state)
        db.commit()
        db.refresh(state)
    return state
