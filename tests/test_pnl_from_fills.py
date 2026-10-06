from backend.engine.bot_engine import BotEngine


def test_pnl_from_leg_fills_long():
    entry = [{"symbol": "A", "side": "buy", "qty": 2, "fill_price": 100}]
    close = [{"symbol": "A", "fill_price": 110}]
    assert BotEngine._pnl_from_leg_fills(entry, close) == 20.0


def test_pnl_from_leg_fills_short():
    entry = [{"symbol": "A", "side": "sell", "qty": 1, "fill_price": 100}]
    close = [{"symbol": "A", "fill_price": 90}]
    assert BotEngine._pnl_from_leg_fills(entry, close) == 10.0


def test_pnl_from_leg_fills_missing():
    assert BotEngine._pnl_from_leg_fills([], []) is None
