import asyncio
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from backend import utils
from backend.exchange import xt as xtmod


def _run(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def test_fetch_price_df_is_concurrent_and_ordered():
    order = []

    class C:
        async def get_ohlc(self, sym, res, bars):
            order.append(("start", sym))
            await asyncio.sleep(0.05)
            order.append(("end", sym))
            base = {"A": 1.0, "B": 2.0}[sym]
            return {"t": [60, 120, 180], "c": [base, base + 1, base + 2]}

    df = _run(utils.fetch_price_df(C(), ["A", "B"], "1", 3))
    assert list(df.columns) == ["A", "B"] and len(df) == 3
    # both started before the first finished -> concurrent
    assert [e[0] for e in order[:2]] == ["start", "start"]


def test_xt_markets_cached_across_clients(monkeypatch):
    monkeypatch.setitem(xtmod._MARKETS_CACHE, "markets", None)
    monkeypatch.setitem(xtmod._MARKETS_CACHE, "ts", 0.0)
    calls = {"n": 0}

    def make():
        c = xtmod.XTClient()
        async def fake_load():
            calls["n"] += 1
            c._ex.markets = {"BTC/USDT:USDT": {"swap": True, "linear": True}}
            c._ex.currencies = {}
        c._ex.load_markets = fake_load
        c._ex.set_markets = MagicMock()
        return c

    a, b = make(), make()
    _run(a._ensure_markets())
    _run(b._ensure_markets())
    assert calls["n"] == 1
    b._ex.set_markets.assert_called_once()
    _run(a.aclose()); _run(b.aclose())


def test_live_fit_cached_per_candle_and_not_persisted(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.db import Base
    from backend import models
    from backend.routers import groups_router as gr

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    g = models.Group(name="t", symbols=["X", "Y"], dependent_symbol="Y", status="active")
    db.add(g); db.commit()

    rng = np.random.default_rng(0)
    x = 100 + np.cumsum(rng.normal(size=120))
    y = 2 * x + rng.normal(size=120)
    ts = list(range(60, 60 * 121, 60))
    fetches = {"n": 0}

    class MD:
        async def get_ohlc(self, sym, res, bars):
            fetches["n"] += 1
            return {"t": ts, "c": list((x if sym == "X" else y))}
        async def aclose(self):
            pass

    monkeypatch.setattr(gr.factory, "build_market_data_client", lambda db: MD())
    monkeypatch.setattr(gr.config_service, "get_section", lambda db, s: {
        "window_size": 100, "sampling_time": 60, "adf_alpha": 0.05, "kpss_alpha": 0.05,
        "stationarity_method": "engle_granger"})
    gr._LIVE_FIT_CACHE.clear()

    async def go():
        a = await gr.live_fit(g.id, False, db)
        n_after_first = fetches["n"]
        b = await gr.live_fit(g.id, False, db)
        return a, b, n_after_first

    a, b, n1 = _run(go())
    assert a["cached"] is False and b["cached"] is True
    assert fetches["n"] == n1 == 2           # second call made no exchange request
    assert db.query(models.OLSFit).count() == 0   # viewing no longer writes DB rows
    assert a["fitted_at"] == b["fitted_at"]
