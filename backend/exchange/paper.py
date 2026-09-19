"""
Paper trading: real public market data (via NobitexClient, no auth needed),
simulated order fills. This is the default mode and what step 4/close logic
runs against until the user explicitly flips to live mode.
"""
from backend.exchange.nobitex import NobitexClient


class PaperExchangeClient:
    def __init__(self, market_data_client: NobitexClient, fee_rate: float = 0.001, slippage_rate: float = 0.0005):
        """
        fee_rate + slippage_rate together model "transaction fee" from the
        spec (trading fee + slippage), applied as a fractional cost against
        the notional of each simulated fill.
        """
        self.md = market_data_client
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate

    async def get_liquid_symbols(self, top_n: int, quote: str = "IRT") -> list[str]:
        return await self.md.get_liquid_symbols(top_n, quote)

    async def get_ohlc(self, symbol: str, resolution: str, bars: int) -> dict:
        return await self.md.get_ohlc(symbol, resolution, bars)

    async def get_last_price(self, symbol: str) -> float:
        return await self.md.get_last_price(symbol)

    async def place_order(self, symbol: str, side: str, amount: float, price: float | None) -> dict:
        """
        Simulated fill at the current last price, adjusted for slippage
        against the trader (buy fills higher, sell fills lower), minus fee.
        Returns a shape similar enough to the real order response for the
        bot engine to treat both paths uniformly.
        """
        last = price if price is not None else await self.get_last_price(symbol)
        slip = last * self.slippage_rate
        fill_price = last + slip if side == "buy" else last - slip
        notional = fill_price * amount
        fee = notional * self.fee_rate
        return {
            "status": "ok",
            "mode": "paper",
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "fill_price": fill_price,
            "fee": fee,
        }

    def total_cost_rate(self) -> float:
        return self.fee_rate + self.slippage_rate
