"""Offline tests for XT base <-> contracts conversion (no live API)."""
import pytest
from unittest.mock import MagicMock, AsyncMock

from backend.exchange.xt import XTClient, XTError


def _client_with_market(contract_size: float, precision_amount: float = 0.001):
    c = XTClient.__new__(XTClient)
    c._ex = MagicMock()
    c._position_mode_warned = True
    market = {
        "contractSize": contract_size,
        "limits": {"amount": {"min": 1}, "cost": {"min": None}},
        "precision": {"amount": 3},
        "info": {},
    }
    c._ex.market = MagicMock(return_value=market)
    # amount_to_precision: floor to 3 decimals like typical ccxt
    def atp(sym, amt):
        return f"{float(amt):.3f}"
    c._ex.amount_to_precision = atp
    c._ensure_markets = AsyncMock()
    c._assert_linear_usdt_swap = MagicMock()
    c._ensure_leverage_margin = AsyncMock()
    c._normalize_symbol = lambda s: s if "/" in s else s
    return c


def test_base_to_contracts_divides_by_contract_size():
    c = _client_with_market(0.01)
    # 0.05 BTC base, contractSize 0.01 -> 5 contracts
    assert c._base_to_contracts("BTC/USDT:USDT", 0.05) == 5.0


def test_base_to_contracts_rejects_zero_after_round():
    c = _client_with_market(100.0)
    # tiny base rounds to 0 contracts
    assert c._base_to_contracts("FOO/USDT:USDT", 0.001) == 0.0


def test_contracts_to_base():
    c = _client_with_market(0.01)
    assert c._contracts_to_base("BTC/USDT:USDT", 5.0) == pytest.approx(0.05)


def test_map_position_liability_is_base_units():
    c = _client_with_market(0.01)
    pos = {
        "contracts": 5,
        "contractSize": 0.01,
        "side": "long",
        "symbol": "BTC/USDT:USDT",
        "entryPrice": 50000,
        "markPrice": 51000,
        "unrealizedPnl": 10,
        "id": "x",
    }
    mapped = c._map_position(pos)
    assert mapped["liability"] == pytest.approx(0.05)
    assert mapped["contracts"] == 5
    assert mapped["contractSize"] == 0.01


def test_map_position_zero_contract_size_defaults_to_one():
    c = _client_with_market(1.0)
    pos = {"contracts": 2, "contractSize": 0, "side": "short", "symbol": "X"}
    mapped = c._map_position(pos)
    assert mapped["liability"] == 2.0
