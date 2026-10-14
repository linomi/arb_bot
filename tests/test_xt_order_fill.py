"""XT: actual fill price / size / fee are read from the exchange's order record."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.strategy.pnl import fills_pnl_and_fee

SYM = "DOGE/USDT:USDT"


def _run(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


@pytest.fixture
def xt():
    with patch("backend.exchange.xt.ccxt") as mod:
        ex = AsyncMock()
        ex.markets = {SYM: {"symbol": SYM, "active": True, "swap": True, "linear": True, "settle": "USDT",
                            "type": "swap", "contractSize": 10, "limits": {"cost": {"min": 5.0}}}}
        ex.market = lambda s: ex.markets[s]
        ex.load_markets = AsyncMock(return_value=ex.markets)
        ex.apiKey = "k"
        mod.xt = MagicMock(return_value=ex)
        from backend.exchange.xt import XTClient
        yield XTClient(api_key="k", api_secret="s"), ex


def test_fill_from_order_converts_contracts_to_base_and_reads_fee(xt):
    c, ex = xt
    ex.fetch_order = AsyncMock(return_value={"status": "closed", "amount": 5, "filled": 5, "average": 0.1234,
                                             "fee": {"cost": 0.0037, "currency": "USDT"}})
    f = _run(c.get_order_fill("o1", SYM, delay=0))
    assert f["price"] == pytest.approx(0.1234) and f["filled_base"] == pytest.approx(50.0)
    assert f["fee"] == pytest.approx(0.0037) and f["complete"]


def test_fill_falls_back_to_trades_when_order_lookup_fails(xt):
    c, ex = xt
    ex.fetch_order = AsyncMock(side_effect=RuntimeError("not found"))
    ex.fetch_my_trades = AsyncMock(return_value=[
        {"order": "o1", "price": 0.10, "amount": 2, "fee": {"cost": 0.001, "currency": "USDT"}},
        {"order": "o1", "price": 0.20, "amount": 2, "fee": {"cost": 0.002, "currency": "USDT"}},
        {"order": "other", "price": 9.0, "amount": 9},
    ])
    f = _run(c.get_order_fill("o1", SYM, attempts=2, delay=0))
    assert f["price"] == pytest.approx(0.15) and f["filled_base"] == pytest.approx(40.0)
    assert f["fee"] == pytest.approx(0.003) and f["source"] == "trades"


def test_fill_is_none_when_exchange_returns_nothing(xt):
    c, ex = xt
    ex.fetch_order = AsyncMock(side_effect=RuntimeError("nope"))
    ex.fetch_my_trades = AsyncMock(return_value=[])
    assert _run(c.get_order_fill("o1", SYM, attempts=2, delay=0)) is None


def test_engine_applies_exchange_fill_to_leg_and_exact_flag(xt):
    from backend.engine.bot_engine import BotEngine
    c, ex = xt
    ex.fetch_order = AsyncMock(return_value={"status": "closed", "amount": 5, "filled": 5, "average": 0.1234,
                                             "fee": {"cost": 0.0037, "currency": "USDT"}})
    leg = {"symbol": SYM, "side": "buy", "qty": 49.0, "price": 0.1230}
    assert _run(BotEngine()._apply_exchange_fill(c, leg, "o1", is_close=False))
    assert leg["fill_price"] == pytest.approx(0.1234) and leg["filled_qty"] == pytest.approx(50.0)
    assert leg["fee_actual"] == pytest.approx(0.0037) and leg["fill_source"] == "exchange_order"
    close = {**leg, "side": "sell", "fee_actual": 0.0040, "fill_price": 0.1250}
    assert BotEngine._legs_exchange_exact([leg], [close])
    assert not BotEngine._legs_exchange_exact([leg], [{**close, "fill_source": None}])


def test_pnl_uses_actual_fees_when_both_fills_have_them_else_the_rate():
    ent = [{"symbol": "A", "side": "buy", "qty": 10, "filled_qty": 10, "fill_price": 100.0, "fee_actual": 0.5}]
    cls = [{"symbol": "A", "side": "sell", "fill_price": 101.0, "fee_actual": 0.6}]
    gross, fee = fills_pnl_and_fee(ent, cls, fee_rate=0.001)
    assert gross == pytest.approx(10.0) and fee == pytest.approx(1.1)
    cls[0].pop("fee_actual")
    _, fee2 = fills_pnl_and_fee(ent, cls, fee_rate=0.001)
    assert fee2 == pytest.approx(0.001 * 10 * 201.0)


def test_place_legs_entry_and_close_use_exchange_fills_end_to_end(xt):
    """Entry then close through the real _place_legs with a mocked ccxt exchange."""
    from backend.engine.bot_engine import BotEngine
    c, ex = xt
    ex.amount_to_precision = lambda s, a: str(round(float(a), 4))
    ex.set_leverage = AsyncMock(return_value={})
    ex.set_margin_mode = AsyncMock(return_value={})
    ex.create_order = AsyncMock(side_effect=[{"id": "e1", "status": "closed"}, {"id": "c1", "status": "closed"}])
    ex.fetch_order = AsyncMock(side_effect=[
        {"status": "closed", "amount": 5, "filled": 5, "average": 0.1234, "fee": {"cost": 0.0037, "currency": "USDT"}},
        {"status": "closed", "amount": 5, "filled": 5, "average": 0.1250, "fee": {"cost": 0.0038, "currency": "USDT"}},
    ])
    pos = {"symbol": SYM, "side": "long", "contracts": 5, "contractSize": 10, "entryPrice": 0.1233,
           "markPrice": 0.1234, "id": "p1"}
    ex.fetch_positions = AsyncMock(side_effect=[[pos], [pos], [pos], [pos], [pos]])
    ex.fetch_ticker = AsyncMock(return_value={"last": 0.1234})
    ent = _run(BotEngine()._place_legs(c, [{"symbol": SYM, "side": "buy", "qty": 50.0, "price": 0.1230}], is_close=False))
    assert ent[0]["fill_source"] == "exchange_order" and ent[0]["fill_price"] == pytest.approx(0.1234)
    assert ent[0]["position_id"] and ent[0]["filled_qty"] == pytest.approx(50.0)
    cl = _run(BotEngine()._place_legs(c, [{"symbol": SYM, "side": "sell", "qty": 50.0, "price": 0.1249,
                                           "position_id": ent[0]["position_id"]}], is_close=True))
    assert cl[0]["fill_source"] == "exchange_order" and cl[0]["fill_price"] == pytest.approx(0.1250)
    assert BotEngine._legs_exchange_exact(ent, cl)
    gross, fee = fills_pnl_and_fee(ent, cl, 0.0006)
    assert gross == pytest.approx((0.1250 - 0.1234) * 50.0) and fee == pytest.approx(0.0075)
