"""Offline tests for per-market Nobitex min order value and preflight ordering."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.exchange.nobitex import NobitexClient
from backend.engine.bot_engine import BotEngine
from backend.strategy.sizing import MIN_ORDER_VALUE_IRT


def test_min_order_value_falls_back_to_global():
    c = NobitexClient.__new__(NobitexClient)
    c._margin_markets_cache = {}
    assert c.min_order_value_irt("BTCIRT") == MIN_ORDER_VALUE_IRT


def test_min_order_value_from_cache_toman_scaled():
    c = NobitexClient.__new__(NobitexClient)
    c._margin_markets_cache = {"BTCIRT": {"minOrderValue": 5000}}  # Toman-like
    v = c.min_order_value_irt("BTCIRT")
    assert v == 50000.0  # *10 Rial


def test_min_order_value_rial_unchanged():
    c = NobitexClient.__new__(NobitexClient)
    c._margin_markets_cache = {"ETHIRT": {"minNotional": 100000}}
    assert c.min_order_value_irt("ETHIRT") == 100000.0


@pytest.mark.asyncio
async def test_preflight_orders_riskiest_first():
    eng = BotEngine()
    client = MagicMock()
    client.min_order_value_irt = MagicMock(side_effect=lambda s: 50000 if "BTC" in s else 200000)
    client.get_margin_markets = AsyncMock(return_value={})
    legs = [
        {"symbol": "BTCIRT", "qty": 0.01, "price": 6_000_000},  # 60k / 50k = 1.2
        {"symbol": "ETHIRT", "qty": 0.1, "price": 2_100_000},   # 210k / 200k = 1.05 riskier
    ]
    ordered = await eng._preflight_legs(client, legs, "nobitex")
    assert ordered[0]["symbol"] == "ETHIRT"
    assert ordered[1]["symbol"] == "BTCIRT"


@pytest.mark.asyncio
async def test_preflight_rejects_below_min():
    eng = BotEngine()
    client = MagicMock()
    client.min_order_value_irt = MagicMock(return_value=50000)
    client.get_margin_markets = AsyncMock(return_value={})
    legs = [{"symbol": "FOOIRT", "qty": 0.001, "price": 1000}]  # 1 IRT
    with pytest.raises(ValueError, match="preflight"):
        await eng._preflight_legs(client, legs, "nobitex")
