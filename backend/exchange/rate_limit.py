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
    """
    Per-key sliding window: at most `max_calls` in `period_sec`.
    Blocks (async sleep) until a slot is free.
    """

    def __init__(self):
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, max_calls: int, period_sec: float):
        # max_calls <= 0 → unlimited (no wait)
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

# ---------------------------------------------------------------------------
# Market data — loose. Init/backtest need many parallel OHLC pulls.
# Nobitex handles ~60+/min on public history; we allow ~120/min with a soft cap.
# ---------------------------------------------------------------------------
MARKET_DATA_LIMITS = {
    "udf_history": (500, 60.0),          # OHLC bars — main init bottleneck
    "market_stats": (500, 60.0),          # volume ranking / last price
    "margin_markets_list": (60, 60.0),   # symbol universe (cached 60s in client)
}

# ---------------------------------------------------------------------------
# Live trading — tight, headroom under documented caps.
# ---------------------------------------------------------------------------
LIVE_TRADING_LIMITS = {
    "margin_orders_add": (280, 600.0),   # docs: 300 / 10 min (shared with spot)
    "positions_list": (25, 600.0),       # docs: 30 / 10 min
    "positions_status": (90, 600.0),     # docs: 100 / 10 min
    "positions_close": (90, 600.0),
    "wallets_transfer": (8, 60.0),       # docs: 10 / min
    "delegation_limit": (10, 60.0),      # docs: 12 / min
}

LIMITS = {**MARKET_DATA_LIMITS, **LIVE_TRADING_LIMITS}

# Keys that skip throttling entirely (set empty or override if needed).
SKIP = set()


async def throttle(key: str):
    if key in SKIP:
        return
    max_calls, period = LIMITS.get(key, (60, 60.0))
    await limiter.acquire(key, max_calls, period)
