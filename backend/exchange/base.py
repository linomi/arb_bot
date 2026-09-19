"""
Interface both the paper engine and the live Nobitex client conform to, so
the bot engine never has to branch on trading mode.
"""
from abc import ABC, abstractmethod


class ExchangeClient(ABC):
    @abstractmethod
    async def get_liquid_symbols(self, top_n: int, quote: str = "IRT") -> list[str]:
        """Return the top-N markets by 24h volume, quoted in `quote`."""

    @abstractmethod
    async def get_ohlc(self, symbol: str, resolution: str, bars: int) -> dict:
        """Return {'t': [...], 'o':[...], 'h':[...], 'l':[...], 'c':[...], 'v':[...]}"""

    @abstractmethod
    async def get_last_price(self, symbol: str) -> float:
        """Latest traded price for `symbol`."""

    @abstractmethod
    async def place_order(self, symbol: str, side: str, amount: float, price: float | None) -> dict:
        """Submit an order. `price=None` -> market order."""
