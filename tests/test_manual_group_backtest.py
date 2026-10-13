import asyncio

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_service
from backend.models import Base
from backend.routers import manual_router
from backend.schemas import ManualGroupCreate

DEP, X = "ETH/USDT:USDT", "BTC/USDT:USDT"


class MD:
    def __init__(self, fail=False):
        self.fail = fail
        rng = np.random.default_rng(7)
        n = 3000
        x = 60000 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
        ou = np.zeros(n)
        for i in range(1, n):
            ou[i] = 0.88 * ou[i - 1] + rng.normal(0, 0.004)
        self.series = {X: x, DEP: 0.04 * x + 25 * ou / ou.std()}

    async def get_ohlc(self, sym, res, bars, end_ts=None):
        if self.fail:
            raise RuntimeError("network down")
        c = self.series[sym]
        return {"t": [1_700_000_000 + 60 * i for i in range(len(c))], "c": list(map(float, c)),
                "o": [], "h": [], "l": [], "v": []}

    async def aclose(self):
        pass


@pytest.fixture
def db():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    d = sessionmaker(bind=eng)()
    config_service.seed_defaults_if_missing(d)
    yield d
    d.close()


def _create(db, monkeypatch, md):
    monkeypatch.setattr(manual_router.factory, "build_market_data_client", lambda _db: md)
    body = ManualGroupCreate(symbols=[DEP, X], dependent_symbol=DEP, exchange="xt", activate=False)
    return asyncio.run(manual_router.create_manual_group(body, db))


def test_manual_group_is_backtested_on_creation(db, monkeypatch):
    out = _create(db, monkeypatch, MD())
    assert out["backtest_ok"] and out["backtest_error"] is None
    m = out["backtest_metrics"]
    assert m and m["trade_count"] > 0 and out["dependent_symbol"] == DEP


def test_manual_group_is_still_created_when_backtest_fails(db, monkeypatch):
    out = _create(db, monkeypatch, MD(fail=True))
    assert out["id"] and out["backtest_metrics"] is None
    assert not out["backtest_ok"] and "network down" in out["backtest_error"]


def test_progress_is_reported_and_finishes(db, monkeypatch):
    from backend import init_progress
    init_progress.reset()
    seen = []
    orig = init_progress.update

    def spy(*a, **k):
        orig(*a, **k)
        seen.append((k.get("phase"), init_progress.get_progress()["percent"]))

    monkeypatch.setattr(init_progress, "update", spy)
    _create(db, monkeypatch, MD())
    phases = [p for p, _ in seen]
    assert "fetch_ohlc" in phases and "backtest" in phases and "persist" in phases
    pcts = [x for _, x in seen]
    assert pcts == sorted(pcts)  # never goes backwards
    p = init_progress.get_progress()
    assert p["phase"] == "done" and p["percent"] == 100 and not p["running"]


def test_manual_create_is_refused_while_another_run_is_active(db, monkeypatch):
    from fastapi import HTTPException
    from backend import init_progress
    init_progress.reset()
    assert init_progress.start("busy")
    with pytest.raises(HTTPException) as e:
        _create(db, monkeypatch, MD())
    assert e.value.status_code == 409
    init_progress.reset()
