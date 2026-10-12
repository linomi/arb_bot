import asyncio
import datetime as dt
import importlib.util
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_service
from backend.engine import bot_engine as be
from backend.models import Base, BotState, Group, OLSFit, Trade
from backend.notify import hub

DEP, X = "ETH/USDT:USDT", "BTC/USDT:USDT"


class Rec:
    def __init__(self):
        self.events = []

    def __call__(self, kind, data):
        self.events.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.events]


@pytest.fixture
def rec():
    r = Rec()
    hub.add_sink(r)
    yield r
    hub.remove_sink(r)


@pytest.fixture
def world(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    sf = sessionmaker(bind=eng)
    db = sf()
    config_service.seed_defaults_if_missing(db)
    db.add(BotState(is_running=True, trading_mode="paper", exchange="xt"))
    g = Group(name="grp", dependent_symbol=DEP, symbols=[DEP, X], exchange="xt", status="active")
    db.add(g); db.flush()
    f = OLSFit(group_id=g.id, betas={X: 0.04}, intercept=0.0, resid_mean=0.0, resid_std=1.0)
    db.add(f); db.flush()
    legs = [{"symbol": DEP, "side": "buy", "qty": 0.01, "price": 2500.0, "filled_qty": 0.01},
            {"symbol": X, "side": "sell", "qty": 0.0004, "price": 60000.0, "filled_qty": 0.0004}]
    t = Trade(group_id=g.id, ols_fit_id=f.id, direction="long_residual", mode="paper",
              entry_time=dt.datetime.utcnow() - dt.timedelta(hours=2), entry_z=-2.2, entry_residual=-2.2,
              entry_prices={DEP: 2500.0, X: 60000.0}, status="open", legs_entry=legs,
              trade_notional=49.0, notional_basis="gross")
    db.add(t); db.commit()
    gid, tid = g.id, t.id
    db.close()

    monkeypatch.setattr(be, "SessionLocal", sf)

    class Client:
        placed = []

        async def place_order(self, symbol, side, qty, **kw):
            Client.placed.append((symbol, side, qty, kw.get("reduce_only")))
            return {"order": {"id": "1"}}

        async def aclose(self):
            pass

    monkeypatch.setattr(be.factory, "build_market_data_client", lambda db: Client())
    monkeypatch.setattr(be.factory, "build_trading_client", lambda db: Client())

    async def fake_prices(client, symbols, resolution, bars):
        return pd.DataFrame({DEP: [2500.0], X: [60000.0]})

    monkeypatch.setattr(be, "fetch_price_df", fake_prices)
    return sf, gid, tid, Client


def test_close_all_stops_entries_and_closes_trades(world, rec):
    sf, gid, tid, Client = world
    res = asyncio.run(be.BotEngine().close_all("manual_closeall"))
    assert res == {"total": 1, "closed": 1, "failed": 0}
    db = sf()
    t = db.get(Trade, tid)
    assert t.status == "closed" and t.close_reason == "manual_closeall"
    assert db.query(BotState).first().is_running is False  # no new entries after close-all
    db.close()
    assert [p[1] for p in Client.placed] == ["sell", "buy"]  # opposite sides of the entry legs
    closed = [d for k, d in rec.events if k == "closed"]
    assert len(closed) == 1 and closed[0]["reason"] == "manual_closeall" and closed[0]["group"] == "grp"


def test_close_all_with_nothing_open(world):
    sf, gid, tid, Client = world
    db = sf(); db.get(Trade, tid).status = "closed"; db.commit(); db.close()
    assert asyncio.run(be.BotEngine().close_all()) == {"total": 0, "closed": 0, "failed": 0}


def test_liquidation_guard_is_wired_into_live_exit(world, rec, monkeypatch):
    sf, gid, tid, Client = world
    db = sf()
    bs = db.query(BotState).first(); bs.trading_mode = "live"
    t = db.get(Trade, tid); t.mode = "live"
    db.commit()
    engine = be.BotEngine()
    called = []

    async def near(trade, client, backbone, **kw):
        called.append(trade.id)
        return True

    monkeypatch.setattr(engine, "_risk_exit_if_near_liquidation", near)
    bb = config_service.get_section(db, "backbone")
    # price where z is nowhere near an exit, so only the risk guard can trigger it
    asyncio.run(engine._check_exit(db, db.get(Group, gid), t, {DEP: 2397.8, X: 60000.0}, bb, Client(), "live", exchange="xt"))
    db.refresh(t)
    assert called == [tid] and t.status == "closed" and t.close_reason == "risk_exit"
    assert "risk_exit" in rec.kinds()
    db.close()


def _guard_setup(sf, gid, mode):
    """Position: long DEP @2500, short X @60000, x2 leverage. resid_std is widened so that moving
    DEP alone keeps z around -2 (no z-based exit), leaving the liquidation guard as the only trigger."""
    db = sf()
    fit = db.query(OLSFit).first(); fit.resid_std = 450.0
    t = db.query(Trade).first()
    t.mode = mode
    db.commit()
    return db, t, config_service.get_section(db, "backbone")


def test_guard_closes_paper_trade_near_modeled_liquidation(world, rec):
    sf, gid, tid, Client = world
    db, t, bb = _guard_setup(sf, gid, "paper")
    # long DEP liq = 2500*(1-1/2+0.005) = 1262.5 ; at 1500 the mark is 15.8% above it (< 20%)
    asyncio.run(be.BotEngine()._check_exit(db, db.get(Group, gid), t, {DEP: 1500.0, X: 60000.0}, bb, Client(), "paper", exchange="xt"))
    db.refresh(t)
    assert t.status == "closed" and t.close_reason == "risk_exit"
    assert "risk_exit" in rec.kinds()
    db.close()


def test_guard_leaves_paper_trade_alone_when_far_from_liquidation(world):
    sf, gid, tid, Client = world
    db, t, bb = _guard_setup(sf, gid, "paper")
    # 1700 is 25.7% above the modeled liquidation price: outside the 20% band
    asyncio.run(be.BotEngine()._check_exit(db, db.get(Group, gid), t, {DEP: 1700.0, X: 60000.0}, bb, Client(), "paper", exchange="xt"))
    db.refresh(t)
    assert t.status == "open"
    db.close()


def test_guard_can_be_switched_off(world):
    sf, gid, tid, Client = world
    db, t, bb = _guard_setup(sf, gid, "paper")
    bb["liquidation_proximity_fraction"] = 0
    asyncio.run(be.BotEngine()._check_exit(db, db.get(Group, gid), t, {DEP: 1500.0, X: 60000.0}, bb, Client(), "paper", exchange="xt"))
    db.refresh(t)
    assert t.status == "open"
    db.close()


def test_live_guard_falls_back_to_model_when_exchange_has_no_data(world):
    sf, gid, tid, Client = world
    db, t, bb = _guard_setup(sf, gid, "live")

    class Blind(Client):
        async def get_position(self, pid):
            return None  # exchange returns nothing

    asyncio.run(be.BotEngine()._check_exit(db, db.get(Group, gid), t, {DEP: 1500.0, X: 60000.0}, bb, Blind(), "live", exchange="xt"))
    db.refresh(t)
    assert t.status == "closed" and t.close_reason == "risk_exit"
    db.close()


def test_live_guard_trusts_exchange_liquidation_price_over_model(world):
    sf, gid, tid, Client = world
    db, t, bb = _guard_setup(sf, gid, "live")
    legs = [dict(l, position_id=l["symbol"]) for l in t.legs_entry]
    t.legs_entry = legs
    db.commit()

    class Reports(Client):
        async def get_position(self, pid):
            # exchange says liquidation is very far away -> no exit, even though the model would fire
            return {"markPrice": "1500" if pid == DEP else "60000", "liquidationPrice": "1" if pid == DEP else "999999"}

    asyncio.run(be.BotEngine()._check_exit(db, db.get(Group, gid), t, {DEP: 1500.0, X: 60000.0}, bb, Reports(), "live", exchange="xt"))
    db.refresh(t)
    assert t.status == "open"
    db.close()


def test_entry_is_blocked_when_leverage_puts_position_inside_guard_band(world):
    sf, gid, tid, Client = world
    db = sf()
    g = db.get(Group, gid)
    bb = dict(config_service.get_section(db, "backbone"))
    legs = [{"symbol": DEP, "side": "buy", "price": 2500.0}, {"symbol": X, "side": "sell", "price": 60000.0}]
    prices = {DEP: 2500.0, X: 60000.0}
    block = be.BotEngine._entry_liquidation_block
    bb["xt_leverage"] = 2
    assert block(g, legs, prices, bb, "xt") is None
    bb["xt_leverage"] = 10
    msg = block(g, legs, prices, bb, "xt")
    assert msg and "x10" in msg
    bb["liquidation_proximity_fraction"] = 0
    assert block(g, legs, prices, bb, "xt") is None
    db.close()


def test_paper_close_pnl_is_net_of_fees_and_divergence_is_informational(world, caplog):
    import logging
    sf, gid, tid, Client = world
    with caplog.at_level(logging.INFO, logger="bot_engine"):
        asyncio.run(be.BotEngine().close_all("manual_closeall"))
    db = sf()
    t = db.get(Trade, tid)
    # entry and close at the same prices: gross fills PnL is 0, so PnL is minus the 0.1% fee on both legs
    fee = 0.001 * (0.01 * (2500 + 2500) + 0.0004 * (60000 + 60000))
    assert t.realized_fee == pytest.approx(fee)
    assert t.pnl == pytest.approx(-fee) and t.realized_pnl == pytest.approx(-fee)
    db.close()
    div = [r for r in caplog.records if "PnL divergence" in r.getMessage()]
    assert div and all(r.levelno == logging.INFO for r in div)  # paper: informational, not a warning
    assert "hedge-ratio drift" in div[0].getMessage()
    assert not any("LIVE PnL" in r.getMessage() for r in caplog.records)


def test_pnl_from_leg_fills_net_of_fees():
    from backend.strategy.pnl import fills_pnl_and_fee
    entry = [{"symbol": "A", "side": "buy", "filled_qty": 2, "fill_price": 100.0}]
    close = [{"symbol": "A", "fill_price": 110.0}]
    gross, fee = fills_pnl_and_fee(entry, close, 0.001)
    assert gross == pytest.approx(20.0) and fee == pytest.approx(0.42)
    assert be.BotEngine._pnl_from_leg_fills(entry, close, 0.001) == pytest.approx(19.58)
    assert be.BotEngine._pnl_from_leg_fills(entry, close) == pytest.approx(20.0)  # old call style still works
