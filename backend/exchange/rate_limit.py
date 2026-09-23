"""
Token-bucket rate limits for Nobitex endpoints.

Market data (OHLC / stats / margin market list) is intentionally loose so
initialization & backtests can fan out ~60+ req/min without stalling.

Live trading endpoints stay conservative vs official OpenAPI caps.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    def __init__(self):
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, max_calls: int, period_sec: float):
        if max_calls <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                window = self._hits[key]
                cutoff = now - period_sec
                self._hits[key] = [t for t in window if t > cutoff]
                window = self._hits[key]
                if len(window) < max_calls:
                    window.append(now)
                    return
                wait = period_sec - (now - window[0]) + 0.02
            await asyncio.sleep(max(0.02, wait))


limiter = RateLimiter()

MARKET_DATA_LIMITS = {
    "udf_history": (60, 60.0),
    "market_stats": (20, 60.0),
    "margin_markets_list": (30, 60.0),
}

LIVE_TRADING_LIMITS = {
    "margin_orders_add": (280, 600.0),
    "positions_list": (25, 600.0),
    "positions_status": (90, 600.0),
    "positions_close": (90, 600.0),
    "wallets_transfer": (8, 60.0),
    "wallets_list": (30, 60.0),
    "delegation_limit": (10, 60.0),
}

LIMITS = {**MARKET_DATA_LIMITS, **LIVE_TRADING_LIMITS}
SKIP = set()


async def throttle(key: str):
    if key in SKIP:
        return
    max_calls, period = LIMITS.get(key, (60, 60.0))
    await limiter.acquire(key, max_calls, period)
