"""
Interface both the paper engine and live exchange clients conform to, so
the bot engine never has to branch on trading mode or exchange identity
beyond factory selection.
"""
from abc import ABC, abstractmethod
from typing import Any


class ExchangeClient(ABC):
    @abstractmethod
    async def get_liquid_symbols(self, top_n: int, quote: str = "IRT") -> list[str]:
        """Return the top-N markets by 24h volume, quoted/settled in `quote`."""

    @abstractmethod
    async def get_ohlc(self, symbol: str, resolution: str, bars: int) -> dict:
        """Return {'t': [...], 'o':[...], 'h':[...], 'l':[...], 'c':[...], 'v':[...]}"""

    @abstractmethod
    async def get_last_price(self, symbol: str) -> float:
        """Latest traded / mark price for `symbol`."""

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        **kwargs: Any,
    ) -> dict:
        """Submit an order. `price=None` -> market order."""

    @abstractmethod
    async def get_position(self, position_id: Any) -> dict:
        """Fetch a single open/closed position by exchange-native id."""

    @abstractmethod
    async def list_positions(self, **kwargs: Any) -> list[dict]:
        """List positions (active by default). Keyword args are exchange-specific."""

    @abstractmethod
    async def close_position(
        self,
        position_id: Any,
        amount: float,
        execution: str = "market",
        price: float | None = None,
        client_order_id: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """Close (reduce) an existing position. Must not flip side."""

    @abstractmethod
    async def resolve_position_id(
        self,
        symbol: str,
        side: str,
        opened_after_iso: str | None = None,
    ) -> Any | None:
        """Match an open position for (symbol, side); return exchange-native id."""

    @abstractmethod
    async def get_active_balance(self, quote: str) -> float | None:
        """Free balance available as margin collateral in `quote` currency."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release underlying HTTP / websocket resources."""

    # Optional: minimum notional for a symbol (exchange-native). Default None
    # means "use caller-supplied min". XTClient overrides with load_markets limits.
    async def get_min_notional(self, symbol: str) -> float | None:
        return None
