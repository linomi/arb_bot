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

    async def near(trade, client, backbone):
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


def test_liquidation_guard_not_used_in_paper(world, monkeypatch):
    sf, gid, tid, Client = world
    db = sf()
    engine = be.BotEngine()

    async def boom(*a, **k):
        raise AssertionError("must not be called in paper mode")

    monkeypatch.setattr(engine, "_risk_exit_if_near_liquidation", boom)
    bb = config_service.get_section(db, "backbone")
    t = db.query(Trade).first()
    asyncio.run(engine._check_exit(db, db.get(Group, gid), t, {DEP: 2397.8, X: 60000.0}, bb, Client(), "paper", exchange="xt"))
    assert t.status == "open"
    db.close()


def test_orphan_event_only_for_new_orphans(world, rec):
    sf, gid, tid, Client = world
    engine = be.BotEngine()

    class C:
        async def list_positions(self):
            return [{"id": "o1", "symbol": "SOL/USDT:USDT", "side": "buy", "liability": 2.0}]

    db = sf()
    asyncio.run(engine.reconcile_orphans(db, C(), "paper", "xt"))
    asyncio.run(engine.reconcile_orphans(db, C(), "paper", "xt"))
    assert rec.kinds().count("orphan") == 1
    db.close()


def test_watchdog_decisions():
    spec = importlib.util.spec_from_file_location("watchdog", Path(__file__).resolve().parent.parent / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec); spec.loader.exec_module(wd)
    assert wd.decide(10, 600, False) is None
    assert wd.decide(700, 600, False) == "down"
    assert wd.decide(None, 600, False) == "down"      # no heartbeat file at all
    assert wd.decide(700, 600, True) is None          # already alerted: stay quiet
    assert wd.decide(5, 600, True) == "up"
