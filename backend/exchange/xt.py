"""
XT.com USDT-M linear perpetual futures client via ccxt.async_support.xt.

Scope (v1):
  - Linear USDT-settled swaps only (market type=swap, linear=True, settle=USDT).
  - One-way (net) position mode — hedge mode is not supported; verify in UI.
  - Market orders only; reduceOnly closes.
  - Leverage / margin mode configured once per symbol per process.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import ccxt.async_support as ccxt
from ccxt.base.errors import RateLimitExceeded, ExchangeError, InvalidOrder

from backend.exchange.base import ExchangeClient

log = logging.getLogger("xt")

# Internal resolution (TradingView/UDF style) -> ccxt timeframe
_RESOLUTION_TO_TF = {
    "1": "1m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "180": "3h",
    "240": "4h",
    "360": "6h",
    "720": "12h",
    "1D": "1d",
    "D": "1d",
    "2D": "2d",
    "3D": "3d",
}

_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "3h": 10800,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "2d": 172800,
    "3d": 259200,
}


class XTError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class XTClient(ExchangeClient):
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        leverage: int = 2,
        margin_mode: str = "isolated",
        timeout: float = 20.0,
    ):
        self._leverage = max(1, int(leverage))
        self._margin_mode = (margin_mode or "isolated").lower()
        if self._margin_mode not in ("isolated", "cross"):
            self._margin_mode = "isolated"

        opts: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "defaultSubType": "linear",
            },
            "timeout": int(timeout * 1000),
        }
        if api_key and api_secret:
            opts["apiKey"] = api_key
            opts["secret"] = api_secret

        self._ex = ccxt.xt(opts)
        # Per-symbol: leverage/margin already configured this process
        self._configured_symbols: set[str] = set()
        self._markets_loaded = False
        self._position_mode_warned = False

    async def aclose(self) -> None:
        try:
            await self._ex.close()
        except Exception as e:
            log.debug("xt close: %s", e)

    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await self._ex.load_markets()
            self._markets_loaded = True

    def _assert_linear_usdt_swap(self, symbol: str) -> dict:
        """Reject anything that is not a linear USDT-settled perpetual swap."""
        market = self._ex.market(symbol)
        if not market.get("swap"):
            raise XTError(
                f"{symbol} is not a swap market (type={market.get('type')}). "
                "Only XT USDT-M linear perpetuals are supported.",
                code="UnsupportedMarket",
            )
        if not market.get("linear"):
            raise XTError(
                f"{symbol} is not linear (inverse/COIN-M is out of scope).",
                code="UnsupportedMarket",
            )
        settle = (market.get("settle") or "").upper()
        if settle != "USDT":
            raise XTError(
                f"{symbol} settles in {settle}, only USDT is supported.",
                code="UnsupportedMarket",
            )
        return market

    def _normalize_symbol(self, symbol: str) -> str:
        """
        Accept either unified swap form ('BTC/USDT:USDT') or a compact form
        ('BTCUSDT') and resolve to the unified swap symbol.
        """
        s = (symbol or "").strip()
        if not s:
            raise XTError("empty symbol")
        # Already unified
        if ":" in s:
            return s
        # Compact BTCUSDT -> BTC/USDT:USDT
        su = s.upper().replace("-", "").replace("_", "")
        if su.endswith("USDT") and len(su) > 4:
            base = su[:-4]
            return f"{base}/USDT:USDT"
        # BTC/USDT without settle suffix
        if "/" in s and ":" not in s:
            return f"{s}:USDT" if not s.upper().endswith(":USDT") else s
        return s

    async def get_liquid_symbols(self, top_n: int, quote: str = "USDT") -> list[str]:
        await self._ensure_markets()
        quote_u = (quote or "USDT").upper()
        if quote_u != "USDT":
            raise XTError(
                f"XT client only supports quote/settle USDT (got {quote_u}).",
                code="UnsupportedQuote",
            )

        candidates: list[str] = []
        for sym, m in self._ex.markets.items():
            if not m.get("active", True):
                continue
            if not m.get("swap") or not m.get("linear"):
                continue
            if (m.get("settle") or "").upper() != "USDT":
                continue
            candidates.append(sym)

        # Rank by quote volume from tickers when available
        vol_by: dict[str, float] = {}
        try:
            tickers = await self._ex.fetch_tickers(candidates[:200] if len(candidates) > 200 else candidates)
            for sym, t in (tickers or {}).items():
                try:
                    vol_by[sym] = float(t.get("quoteVolume") or t.get("baseVolume") or 0)
                except (TypeError, ValueError):
                    vol_by[sym] = 0.0
        except Exception as e:
            log.warning("fetch_tickers failed, returning unsorted: %s", e)

        ranked = sorted(candidates, key=lambda s: vol_by.get(s, 0.0), reverse=True)
        return ranked[: max(1, int(top_n))]

    def _map_resolution(self, resolution: str) -> str:
        r = (resolution or "60").strip()
        if r in _RESOLUTION_TO_TF:
            return _RESOLUTION_TO_TF[r]
        if r.upper().endswith("D"):
            return _RESOLUTION_TO_TF.get(r.upper(), "1d")
        # numeric minutes
        try:
            mins = int(r)
            if mins >= 1440:
                return "1d"
            for key in ("720", "360", "240", "180", "60", "30", "15", "5", "1"):
                if mins >= int(key):
                    return _RESOLUTION_TO_TF[key]
        except ValueError:
            pass
        return "1h"

    async def get_ohlc(self, symbol: str, resolution: str, bars: int) -> dict:
        await self._ensure_markets()
        unified = self._normalize_symbol(symbol)
        self._assert_linear_usdt_swap(unified)

        tf = self._map_resolution(resolution)
        tf_sec = _TF_SECONDS.get(tf, 3600)
        limit = max(1, int(bars))
        since = int(time.time() * 1000) - limit * tf_sec * 1000

        ohlcv = await self._ex.fetch_ohlcv(unified, timeframe=tf, since=since, limit=limit)
        out: dict = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        for row in ohlcv or []:
            # ccxt: [timestamp_ms, open, high, low, close, volume]
            ts_ms, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
            out["t"].append(int(ts_ms) // 1000)
            out["o"].append(float(o) if o is not None else None)
            out["h"].append(float(h) if h is not None else None)
            out["l"].append(float(l) if l is not None else None)
            out["c"].append(float(c) if c is not None else None)
            out["v"].append(v)
        return out

    async def get_last_price(self, symbol: str) -> float:
        await self._ensure_markets()
        unified = self._normalize_symbol(symbol)
        self._assert_linear_usdt_swap(unified)
        ticker = await self._ex.fetch_ticker(unified)
        last = ticker.get("last") or ticker.get("close") or ticker.get("mark")
        if last is None:
            raise XTError(f"No last price for {unified}")
        return float(last)

    async def _ensure_leverage_margin(self, symbol: str) -> None:
        """Call setLeverage / setMarginMode at most once per symbol per process."""
        if symbol in self._configured_symbols:
            return
        try:
            await self._ex.set_margin_mode(self._margin_mode, symbol)
            log.info("XT set_margin_mode(%s, %s)", self._margin_mode, symbol)
        except Exception as e:
            # Already set / not required is common; log and continue
            log.warning("set_margin_mode %s %s: %s", self._margin_mode, symbol, e)
        try:
            await self._ex.set_leverage(self._leverage, symbol)
            log.info("XT set_leverage(%s, %s)", self._leverage, symbol)
        except Exception as e:
            log.warning("set_leverage %s %s: %s", self._leverage, symbol, e)
        self._configured_symbols.add(symbol)

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        client_order_id: str | None = None,
        ref_price: float | None = None,
        reduce_only: bool = False,
        **_kwargs: Any,
    ) -> dict:
        await self._ensure_markets()
        unified = self._normalize_symbol(symbol)
        self._assert_linear_usdt_swap(unified)

        if not self._position_mode_warned and self._ex.apiKey:
            # One-time startup note: hedge mode must be disabled in XT UI
            log.warning(
                "XT live: ensure account position mode is one-way (net), not hedge/dual-side. "
                "Hedge mode is not supported in v1; switch in the XT UI if needed."
            )
            self._position_mode_warned = True

        await self._ensure_leverage_margin(unified)

        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            raise XTError(f"invalid side: {side}")

        # Round amount to exchange precision — XT rejects bad step sizes
        amount_str = self._ex.amount_to_precision(unified, amount)
        amount_f = float(amount_str)
        if amount_f <= 0:
            raise XTError(f"amount rounds to zero for {unified} (raw={amount})")

        params: dict[str, Any] = {}
        if reduce_only:
            params["reduceOnly"] = True
        if client_order_id:
            params["clientOrderId"] = str(client_order_id)[:32]

        order_type = "market" if price is None else "limit"
        try:
            if order_type == "market":
                order = await self._ex.create_order(
                    unified, "market", side_l, amount_f, None, params
                )
            else:
                order = await self._ex.create_order(
                    unified, "limit", side_l, amount_f, float(price), params
                )
        except RateLimitExceeded as e:
            raise  # let bot_engine handle account-wide pause
        except (ExchangeError, InvalidOrder) as e:
            raise XTError(str(e), code="order_failed", payload={"raw": str(e)}) from e

        return {
            "status": "ok",
            "mode": "live",
            "exchange": "xt",
            "symbol": unified,
            "side": side_l,
            "amount": amount_f,
            "order": order,
            "clientOrderId": client_order_id,
        }

    def _map_position(self, pos: dict) -> dict:
        """
        Map ccxt unified position to the subset of fields bot_engine reads
        from Nobitex position dicts: id, side, status, entryPrice, markPrice,
        liability (contracts), PNL / unrealizedPNL, openedAt.
        """
        contracts = float(pos.get("contracts") or 0)
        contract_size = float(pos.get("contractSize") or 1)
        # liability ≈ absolute position size in base contracts (what we close)
        liability = abs(contracts)
        side_raw = (pos.get("side") or "").lower()
        if side_raw in ("long", "buy"):
            side = "buy"
        elif side_raw in ("short", "sell"):
            side = "sell"
        else:
            side = side_raw or ("buy" if contracts > 0 else "sell")

        entry = pos.get("entryPrice")
        mark = pos.get("markPrice")
        upnl = pos.get("unrealizedPnl")
        # Use symbol as stable id when exchange id is absent (one-way mode)
        pid = pos.get("id") or pos.get("symbol") or ""

        status = "active" if liability > 0 else "closed"
        return {
            "id": pid,
            "symbol": pos.get("symbol"),
            "side": side,
            "status": status,
            "entryPrice": entry,
            "markPrice": mark,
            "exitPrice": mark,  # approximation for open positions
            "liability": liability,
            "contracts": contracts,
            "contractSize": contract_size,
            "PNL": upnl,
            "unrealizedPNL": upnl,
            "openedAt": pos.get("timestamp") or pos.get("datetime"),
            "createdAt": pos.get("timestamp") or pos.get("datetime"),
            "_raw": pos,
        }

    async def list_positions(self, **kwargs: Any) -> list[dict]:
        await self._ensure_markets()
        try:
            positions = await self._ex.fetch_positions()
        except RateLimitExceeded:
            raise
        except Exception as e:
            raise XTError(f"fetch_positions failed: {e}") from e

        out = []
        for p in positions or []:
            contracts = float(p.get("contracts") or 0)
            if abs(contracts) < 1e-12:
                continue
            mapped = self._map_position(p)
            # Optional filters
            if kwargs.get("symbol"):
                want = self._normalize_symbol(str(kwargs["symbol"]))
                if mapped.get("symbol") != want:
                    continue
            out.append(mapped)
        return out

    async def get_position(self, position_id: Any) -> dict:
        """position_id may be a symbol string (one-way) or exchange id."""
        await self._ensure_markets()
        positions = await self.list_positions()
        pid = str(position_id)
        for p in positions:
            if str(p.get("id")) == pid or str(p.get("symbol")) == pid:
                return p
        # Also try resolving as symbol
        try:
            unified = self._normalize_symbol(pid)
            for p in positions:
                if p.get("symbol") == unified:
                    return p
        except Exception:
            pass
        return {"id": position_id, "status": "closed", "liability": 0}

    async def close_position(
        self,
        position_id: Any,
        amount: float,
        execution: str = "market",
        price: float | None = None,
        client_order_id: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """
        Close by placing an opposite-side market order with reduceOnly=True
        for the actual open size (not a blind opposite place_order).
        """
        pos = await self.get_position(position_id)
        liab = float(pos.get("liability") or 0)
        if liab <= 0 or str(pos.get("status", "")).lower() in ("closed", "liquidated"):
            return {"status": "ok", "already_closed": True, "position": pos}

        symbol = pos.get("symbol") or str(position_id)
        unified = self._normalize_symbol(str(symbol))
        side = str(pos.get("side") or "buy").lower()
        close_side = "sell" if side in ("buy", "long") else "buy"
        close_amount = min(float(amount), liab) if amount else liab
        if close_amount <= 0:
            close_amount = liab

        return await self.place_order(
            unified,
            close_side,
            close_amount,
            price=None if execution == "market" else price,
            client_order_id=client_order_id,
            reduce_only=True,
        )

    async def resolve_position_id(
        self,
        symbol: str,
        side: str,
        opened_after_iso: str | None = None,
    ) -> Any | None:
        await self._ensure_markets()
        unified = self._normalize_symbol(symbol)
        positions = await self.list_positions()
        side_l = side.lower()
        side_aliases = {side_l}
        if side_l in ("buy", "long"):
            side_aliases |= {"buy", "long"}
        if side_l in ("sell", "short"):
            side_aliases |= {"sell", "short"}

        candidates = [
            p for p in positions
            if p.get("symbol") == unified
            and str(p.get("side", "")).lower() in side_aliases
            and float(p.get("liability") or 0) > 0
        ]
        if not candidates:
            return None
        # Prefer most recent if timestamps exist
        candidates.sort(
            key=lambda p: str(p.get("openedAt") or p.get("createdAt") or ""),
            reverse=True,
        )
        return candidates[0].get("id") or candidates[0].get("symbol")

    async def get_active_balance(self, quote: str) -> float | None:
        """Free USDT in the futures/swap wallet."""
        quote_u = (quote or "USDT").upper()
        if quote_u != "USDT":
            return None
        try:
            bal = await self._ex.fetch_balance({"type": "swap"})
        except Exception:
            try:
                bal = await self._ex.fetch_balance()
            except Exception as e:
                log.warning("fetch_balance failed: %s", e)
                return None

        # Prefer free USDT
        free = None
        if isinstance(bal.get("free"), dict):
            free = bal["free"].get("USDT") or bal["free"].get("usdt")
        if free is None and "USDT" in bal:
            free = (bal["USDT"] or {}).get("free")
        if free is None:
            free = bal.get("USDT")
        try:
            return float(free) if free is not None else None
        except (TypeError, ValueError):
            return None

    # Backward-compatible alias used by older bot_engine paths
    async def get_margin_active_balance_irt(self) -> float | None:
        return await self.get_active_balance("USDT")

    async def get_min_notional(self, symbol: str) -> float | None:
        await self._ensure_markets()
        unified = self._normalize_symbol(symbol)
        market = self._ex.market(unified)
        limits = market.get("limits") or {}
        cost = (limits.get("cost") or {}).get("min")
        if cost is not None:
            try:
                return float(cost)
            except (TypeError, ValueError):
                pass
        amount_min = (limits.get("amount") or {}).get("min")
        if amount_min is not None:
            try:
                # approximate notional from last price
                px = await self.get_last_price(unified)
                return float(amount_min) * float(px)
            except Exception:
                return None
        return None

    async def fetch_funding_rate_history(
        self, symbol: str, limit: int = 20
    ) -> list[dict]:
        """Expose recent funding rates for tuning funding_rate_estimate."""
        await self._ensure_markets()
        unified = self._normalize_symbol(symbol)
        self._assert_linear_usdt_swap(unified)
        try:
            rows = await self._ex.fetch_funding_rate_history(unified, limit=limit)
            return list(rows or [])
        except Exception as e:
            raise XTError(f"fetch_funding_rate_history failed: {e}") from e
