"""
Helpers for XT-aware behaviour in bot_engine without forking every path.

- effective_cost_rate: fee + slip + funding (funding only when exchange=xt)
- handle_rate_limit: set bot-wide pause_until on RateLimitExceeded
- resolve_min_order_value: per-market min from client, else MIN_ORDER_VALUE_IRT
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

log = logging.getLogger("bot_engine")

XT_RATE_LIMIT_PAUSE_SEC = 60


def effective_cost_rate(
    backbone: dict,
    *,
    exchange: str = "nobitex",
) -> float:
    fee = float(backbone.get("fee_rate", 0.001))
    slip = float(backbone.get("slippage_rate", 0.0005))
    cost = fee + slip
    if (exchange or "").lower() == "xt":
        funding = float(backbone.get("funding_rate_estimate", 0.0) or 0.0)
        intervals = float(backbone.get("expected_holding_funding_intervals", 1) or 0)
        cost += funding * intervals
    return cost


def handle_rate_limit(engine: Any, exc: BaseException, *, exchange: str) -> bool:
    name = type(exc).__name__
    msg = str(exc).lower()
    is_rl = (
        "RateLimit" in name
        or "rate limit" in msg
        or "ratelimit" in msg
        or "too many requests" in msg
    )
    if not is_rl:
        return False
    if (exchange or "").lower() != "xt":
        return False
    until = dt.datetime.utcnow() + dt.timedelta(seconds=XT_RATE_LIMIT_PAUSE_SEC)
    engine._pause_until = until.isoformat() + "Z"
    log.error(
        "XT rate-limit lockout — pausing bot-wide until %s (no immediate retry)",
        engine._pause_until,
    )
    return True


def is_paused(engine: Any) -> bool:
    raw = getattr(engine, "_pause_until", None)
    if not raw:
        return False
    try:
        until = dt.datetime.fromisoformat(str(raw).replace("Z", ""))
        return dt.datetime.utcnow() < until
    except Exception:
        return False


async def resolve_min_order_value(
    trading_client: Any,
    symbols: list[str],
    *,
    exchange: str = "nobitex",
    default_irt: float = 50_000.0,
) -> float:
    if (exchange or "").lower() != "xt":
        return float(default_irt)
    mins: list[float] = []
    if hasattr(trading_client, "get_min_notional"):
        for sym in symbols:
            try:
                m = await trading_client.get_min_notional(sym)
                if m is not None and float(m) > 0:
                    mins.append(float(m))
            except Exception as e:
                log.debug("get_min_notional(%s): %s", sym, e)
    if mins:
        return max(mins)
    return 5.0


async def read_free_balance(
    trading_client: Any,
    *,
    exchange: str = "nobitex",
    quote: str | None = None,
) -> float | None:
    q = quote or ("USDT" if (exchange or "").lower() == "xt" else "IRT")
    if hasattr(trading_client, "get_active_balance"):
        try:
            return await trading_client.get_active_balance(q)
        except Exception as e:
            log.warning("get_active_balance failed: %s", e)
    if hasattr(trading_client, "get_margin_active_balance_irt"):
        try:
            return await trading_client.get_margin_active_balance_irt()
        except Exception as e:
            log.warning("get_margin_active_balance_irt failed: %s", e)
    return None
