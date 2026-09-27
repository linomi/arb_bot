"""Unit tests for Nobitex market-data Rial/Toman scale (Task A)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.exchange.nobitex import NobitexClient, NOBITEX_MARKET_DATA_SCALE


@pytest.mark.asyncio
async def test_get_last_price_scaled():
    client = NobitexClient()
    mock_stats = {"btc-rls": {"latest": "6500000", "mark": "6500000"}}
    with patch.object(client, "get_market_stats", new=AsyncMock(return_value=mock_stats)):
        px = await client.get_last_price("BTCIRT")
    assert px == pytest.approx(6500000 * NOBITEX_MARKET_DATA_SCALE)
    assert NOBITEX_MARKET_DATA_SCALE == 10.0


@pytest.mark.asyncio
async def test_get_ohlc_scaled():
    client = NobitexClient()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "s": "ok",
        "t": [1, 2],
        "o": [100.0, 110.0],
        "h": [120.0, 130.0],
        "l": [90.0, 100.0],
        "c": [110.0, 120.0],
        "v": [1.0, 2.0],
    }
    with patch.object(client, "_request", new=AsyncMock(return_value=mock_resp)):
        with patch("backend.exchange.nobitex.throttle", new=AsyncMock()):
            out = await client.get_ohlc("BTCIRT", "60", 2)
    assert out["c"][0] == pytest.approx(110.0 * NOBITEX_MARKET_DATA_SCALE)
    assert out["o"][1] == pytest.approx(110.0 * NOBITEX_MARKET_DATA_SCALE)
    assert out["v"][0] == 1.0
