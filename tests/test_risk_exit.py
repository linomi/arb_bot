import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.engine.bot_engine import BotEngine


@pytest.mark.asyncio
async def test_risk_exit_near_liq():
    eng = BotEngine()
    trade = MagicMock()
    trade.id = 1
    trade.legs_entry = [{"position_id": "p1", "symbol": "X"}]
    client = MagicMock()
    client.get_position = AsyncMock(return_value={
        "markPrice": "100", "liquidationPrice": "95", "liability": 1,
    })
    assert await eng._risk_exit_if_near_liquidation(trade, client, {"liquidation_proximity_fraction": 0.20})


@pytest.mark.asyncio
async def test_risk_exit_far_ok():
    eng = BotEngine()
    trade = MagicMock()
    trade.id = 1
    trade.legs_entry = [{"position_id": "p1", "symbol": "X"}]
    client = MagicMock()
    client.get_position = AsyncMock(return_value={
        "markPrice": "100", "liquidationPrice": "50", "liability": 1,
    })
    assert not await eng._risk_exit_if_near_liquidation(trade, client, {"liquidation_proximity_fraction": 0.20})
