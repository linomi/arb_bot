import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.engine.bot_engine import BotEngine


@pytest.mark.asyncio
async def test_reconcile_detects_orphan():
    eng = BotEngine()
    pos = [{"id": "p1", "symbol": "BTC/USDT", "liability": 1.0, "side": "buy"}]
    client = MagicMock()
    client.list_positions = AsyncMock(return_value=pos)

    class Q:
        def filter(self, *a, **k):
            return self
        def all(self):
            return []
    class DB:
        def query(self, *a, **k):
            return Q()

    orphans = await eng.reconcile_orphans(DB(), client, "live", "xt")
    assert len(orphans) == 1
    assert "BTC/USDT" in eng.symbols_blocked_by_orphans()


@pytest.mark.asyncio
async def test_reconcile_skips_covered():
    eng = BotEngine()
    pos = [{"id": "p1", "symbol": "ETHIRT", "liability": 2.0, "side": "sell"}]
    client = MagicMock()
    client.list_positions = AsyncMock(return_value=pos)

    class Trade:
        legs_entry = [{"position_id": "p1", "symbol": "ETHIRT"}]
        id = 1
    class Q:
        def filter(self, *a, **k):
            return self
        def all(self):
            return [Trade()]
    class DB:
        def query(self, *a, **k):
            return Q()

    orphans = await eng.reconcile_orphans(DB(), client, "live", "nobitex")
    assert orphans == []
