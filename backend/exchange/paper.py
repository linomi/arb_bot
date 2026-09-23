"""
Paper trading: real public market data, simulated fills.
"""
from backend.exchange.nobitex import NobitexClient


class PaperExchangeClient:
    def __init__(self, market_data_client: NobitexClient, fee_rate: float = 0.001, slippage_rate: float = 0.0005):
        self.md = market_data_client
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate

    async def get_liquid_symbols(self, top_n: int, quote: str = "IRT") -> list[str]:
        return await self.md.get_liquid_symbols(top_n, quote)

    async def get_ohlc(self, symbol: str, resolution: str, bars: int) -> dict:
        return await self.md.get_ohlc(symbol, resolution, bars)

    async def get_last_price(self, symbol: str) -> float:
        return await self.md.get_last_price(symbol)

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        client_order_id: str | None = None,
        ref_price: float | None = None,
        **_kwargs,
    ) -> dict:
        last = price if price is not None else (ref_price if ref_price is not None else await self.get_last_price(symbol))
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
            "matchedAmount": amount,
            "fill_price": fill_price,
            "fee": fee,
            "clientOrderId": client_order_id,
            "order": {"id": f"paper-{symbol}-{side}", "matchedAmount": str(amount)},
        }

    def total_cost_rate(self) -> float:
        return self.fee_rate + self.slippage_rate
