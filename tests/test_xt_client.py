"""
Unit tests for XTClient with a mocked ccxt.async_support.xt.
Does not hit the network.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    # A fresh loop per call, not asyncio.get_event_loop(): this file runs
    # alongside pytest-asyncio-managed tests (test_market_data_scale.py) in
    # the same session, and get_event_loop() can return a loop that
    # pytest-asyncio has already closed depending on collection order.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def mock_ccxt_xt():
    with patch("backend.exchange.xt.ccxt") as mod:
        ex = AsyncMock()
        ex.markets = {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "active": True,
                "swap": True,
                "linear": True,
                "settle": "USDT",
                "type": "swap",
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.001}},
            },
            "ETH/USDT:USDT": {
                "symbol": "ETH/USDT:USDT",
                "active": True,
                "swap": True,
                "linear": True,
                "settle": "USDT",
                "type": "swap",
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.01}},
            },
            # Inverse should be rejected
            "BTC/USD:BTC": {
                "symbol": "BTC/USD:BTC",
                "active": True,
                "swap": True,
                "linear": False,
                "settle": "BTC",
                "type": "swap",
            },
        }
        ex.market = lambda s: ex.markets[s]
        ex.amount_to_precision = lambda s, a: str(round(float(a), 4))
        ex.apiKey = "test"
        ex.load_markets = AsyncMock(return_value=ex.markets)
        ex.fetch_ohlcv = AsyncMock(
            return_value=[
                [1_700_000_000_000, 100.0, 110.0, 90.0, 105.0, 1000],
                [1_700_000_060_000, 105.0, 115.0, 95.0, 110.0, 1100],
            ]
        )
        ex.fetch_ticker = AsyncMock(return_value={"last": 110.0})
        ex.fetch_tickers = AsyncMock(
            return_value={
                "BTC/USDT:USDT": {"quoteVolume": 1e9},
                "ETH/USDT:USDT": {"quoteVolume": 5e8},
            }
        )
        ex.create_order = AsyncMock(return_value={"id": "ord-1", "status": "closed"})
        ex.set_leverage = AsyncMock(return_value={})
        ex.set_margin_mode = AsyncMock(return_value={})
        ex.fetch_positions = AsyncMock(
            return_value=[
                {
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "contracts": 0.01,
                    "contractSize": 1,
                    "entryPrice": 100.0,
                    "markPrice": 105.0,
                    "unrealizedPnl": 0.05,
                    "id": "pos-1",
                }
            ]
        )
        ex.fetch_balance = AsyncMock(
            return_value={"free": {"USDT": 1000.0}, "USDT": {"free": 1000.0}}
        )
        ex.close = AsyncMock()
        mod.xt = MagicMock(return_value=ex)
        yield ex


def test_get_ohlc_shape(mock_ccxt_xt):
    from backend.exchange.xt import XTClient

    client = XTClient(api_key="k", api_secret="s")
    ohlc = _run(client.get_ohlc("BTC/USDT:USDT", "60", 2))
    assert set(ohlc.keys()) == {"t", "o", "h", "l", "c", "v"}
    assert len(ohlc["t"]) == 2
    assert ohlc["t"][0] == 1_700_000_000  # ms -> s
    assert ohlc["c"][1] == 110.0


def test_close_position_reduce_only(mock_ccxt_xt):
    from backend.exchange.xt import XTClient

    client = XTClient(api_key="k", api_secret="s")
    _run(client.close_position("pos-1", amount=0.01))
    args, kwargs = mock_ccxt_xt.create_order.call_args
    # create_order(symbol, type, side, amount, price, params)
    assert args[2] == "sell"  # opposite of long
    params = args[5] if len(args) > 5 else kwargs.get("params", {})
    assert params.get("reduceOnly") is True


def test_leverage_setup_once_per_symbol(mock_ccxt_xt):
    from backend.exchange.xt import XTClient

    client = XTClient(api_key="k", api_secret="s", leverage=3, margin_mode="isolated")
    _run(client.place_order("BTC/USDT:USDT", "buy", 0.01))
    _run(client.place_order("BTC/USDT:USDT", "buy", 0.01))
    assert mock_ccxt_xt.set_leverage.await_count == 1
    assert mock_ccxt_xt.set_margin_mode.await_count == 1


def test_rejects_inverse(mock_ccxt_xt):
    from backend.exchange.xt import XTClient, XTError

    client = XTClient()
    with pytest.raises(XTError, match="not linear"):
        _run(client.get_ohlc("BTC/USD:BTC", "60", 10))


def test_rate_limit_sets_pause_until():
    from backend.engine.xt_hooks import handle_rate_limit

    class FakeRL(Exception):
        pass

    FakeRL.__name__ = "RateLimitExceeded"

    class Eng:
        pass

    eng = Eng()
    handled = handle_rate_limit(eng, FakeRL("too many"), exchange="xt")
    assert handled is True
    assert eng._pause_until is not None

    # Non-XT should not set pause
    eng2 = Eng()
    handled2 = handle_rate_limit(eng2, FakeRL("too many"), exchange="nobitex")
    assert handled2 is False
    assert not hasattr(eng2, "_pause_until") or getattr(eng2, "_pause_until", None) is None


def test_effective_cost_rate_funding_only_for_xt():
    from backend.engine.xt_hooks import effective_cost_rate

    backbone = {
        "fee_rate": 0.001,
        "slippage_rate": 0.0005,
        "funding_rate_estimate": 0.0001,
        "expected_holding_funding_intervals": 2,
    }
    nob = effective_cost_rate(backbone, exchange="nobitex")
    xt = effective_cost_rate(backbone, exchange="xt")
    assert abs(nob - 0.0015) < 1e-12
    assert abs(xt - (0.0015 + 0.0002)) < 1e-12
