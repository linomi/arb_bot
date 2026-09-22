"""Simple token-bucket rate limits for Nobitex endpoints (from official docs)."""
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
        while True:
            async with self._lock:
                now = time.monotonic()
                window = self._hits[key]
                # drop expired
                cutoff = now - period_sec
                self._hits[key] = [t for t in window if t > cutoff]
                window = self._hits[key]
                if len(window) < max_calls:
                    window.append(now)
                    return
                wait = period_sec - (now - window[0]) + 0.05
            await asyncio.sleep(max(0.05, wait))


# Shared process-wide limiter (one bot process).
limiter = RateLimiter()

# Limits from Nobitex margin OpenAPI (conservative).
LIMITS = {
    "margin_markets_list": (30, 60.0),       # 30 / min
    "margin_orders_add": (280, 600.0),       # 300 / 10 min shared with spot — leave headroom
    "positions_list": (25, 600.0),           # 30 / 10 min
    "positions_status": (90, 600.0),         # 100 / 10 min
    "positions_close": (90, 600.0),          # treat like status
    "wallets_transfer": (8, 60.0),           # 10 / min
    "delegation_limit": (10, 60.0),          # 12 / min
    "market_stats": (40, 60.0),              # public; be polite
    "udf_history": (40, 60.0),
}


async def throttle(key: str):
    max_calls, period = LIMITS.get(key, (30, 60.0))
    await limiter.acquire(key, max_calls, period)
