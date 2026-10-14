"""TEMPORARY order-test router (delete with the feature): caps, confirm, open -> close flow on a mocked XT."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers import ordertest_router as ot

SYM = "DOGE/USDT:USDT"


def _run(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "_LOG_PATH", tmp_path / "log.jsonl")
    monkeypatch.setattr(ot, "_STATE_PATH", tmp_path / "state.json")
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    with patch("backend.exchange.xt.ccxt") as mod:
        ex = AsyncMock()
        ex.markets = {SYM: {"symbol": SYM, "active": True, "swap": True, "linear": True, "settle": "USDT",
                            "type": "swap", "contractSize": 10, "limits": {"cost": {"min": 5.0}, "leverage": {"max": 50}},
                            "precision": {"amount": 1}}}
        ex.market = lambda s: ex.markets[s]
        ex.load_markets = AsyncMock(return_value=ex.markets)
        ex.amount_to_precision = lambda s, a: str(a)
        ex.apiKey = "k"
        ex.fetch_ticker = AsyncMock(return_value={"last": 0.1, "bid": 0.0999, "ask": 0.1001, "info": {}})
        ex.fetch_order_book = AsyncMock(return_value={"bids": [[0.0999, 1000]], "asks": [[0.1001, 1000]]})
        ex.fetch_balance = AsyncMock(return_value={"USDT": {"total": 100, "free": 90, "used": 10}})
        ex.fetch_positions = AsyncMock(return_value=[])
        ex.create_order = AsyncMock(return_value={"id": "o1", "status": "closed", "amount": 10, "filled": 10,
                                                  "average": 0.1003, "fee": {"cost": 0.0006, "currency": "USDT"}})
        ex.fetch_order = AsyncMock(return_value={"id": "o1", "status": "closed", "amount": 10, "filled": 10,
                                                 "average": 0.1003, "fee": {"cost": 0.0006, "currency": "USDT"}})
        mod.xt = MagicMock(return_value=ex)
        from backend.exchange.xt import XTClient

        def build(_db, lev):
            return XTClient(api_key="k", api_secret="s", leverage=lev)
        monkeypatch.setattr(ot, "_build_client", build)
        yield db, ex


def test_requires_confirm_and_caps(env):
    db, _ = env
    for kw in ({"confirm": False}, {"notional": ot.MAX_NOTIONAL + 1}, {"leverage": ot.MAX_LEVERAGE + 1}):
        args = dict(symbol=SYM, side="long", notional=10, leverage=3, confirm=True)
        args.update(kw)
        with pytest.raises(HTTPException) as e:
            _run(ot.open_position(ot.OpenIn(**args), db))
        assert e.value.status_code == 400


def test_close_without_open_is_refused(env):
    db, _ = env
    with pytest.raises(HTTPException):
        _run(ot.close_position(ot.CloseIn(symbol=SYM, confirm=True), db))


def test_open_records_slippage_and_state(env):
    db, ex = env
    ev = _run(ot.open_position(ot.OpenIn(symbol=SYM, side="long", notional=10, leverage=3, confirm=True), db))
    assert ev["ok"] is True, ev
    assert ev["analysis"]
    assert SYM in ot.active_symbols()
    assert (ot._LOG_PATH).read_text().strip()
    # second open on the same symbol refused
    with pytest.raises(HTTPException) as e:
        _run(ot.open_position(ot.OpenIn(symbol=SYM, side="long", notional=10, leverage=3, confirm=True), db))
    assert e.value.status_code == 409


def test_open_then_close_clears_state_and_logs_summary(env):
    db, ex = env
    _run(ot.open_position(ot.OpenIn(symbol=SYM, side="short", notional=10, leverage=3, confirm=True), db))
    ex.create_order.return_value = {"id": "o2", "status": "closed", "amount": 10, "filled": 10, "average": 0.1001,
                                    "fee": {"cost": 0.0006, "currency": "USDT"}}
    ex.fetch_order.return_value = dict(ex.create_order.return_value)
    ev = _run(ot.close_position(ot.CloseIn(symbol=SYM, confirm=True), db))
    assert ev["ok"] is True, ev
    assert SYM not in ot.active_symbols()
    assert ev.get("summary")
