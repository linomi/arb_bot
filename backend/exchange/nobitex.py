"""
Live Nobitex REST client — margin markets + margin orders + positions.

Rate limits (from official OpenAPI) are enforced via backend.exchange.rate_limit.
"""
from __future__ import annotations

import asyncio
import time
import base64
import httpx

from backend.exchange.rate_limit import throttle

BASE_URL_DEFAULT = "https://apiv2.nobitex.ir"

# Hard floor for IRT margin order value (user + exchange practice).
MIN_ORDER_VALUE_IRT = 50_000.0


class NobitexError(RuntimeError):
    pass


class NobitexClient:
    def __init__(self, base_url: str = BASE_URL_DEFAULT, token: str | None = None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "TraderBot/StatArb-1.0"},
        )
        self._margin_markets_cache: dict | None = None
        self._margin_markets_cache_ts: float = 0.0

    async def aclose(self):
        await self._client.aclose()

    def _auth_headers(self) -> dict:
        if not self.token:
            raise NobitexError("No API token configured for live trading. Add one in Settings -> Credentials.")
        return {"Authorization": f"Token {self.token}"}

    def _optional_auth_headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Token {self.token}"}
        return {}

    # ---------------------------------------------------------------- public

    async def get_margin_markets(self, details: bool = True, force: bool = False) -> dict:
        now = time.time()
        if (
            not force
            and self._margin_markets_cache is not None
            and (now - self._margin_markets_cache_ts) < 60
        ):
            return self._margin_markets_cache

        await throttle("margin_markets_list")
        r = await self._client.request(
            "GET",
            "/margin/markets/list",
            json={"details": bool(details)},
            headers=self._optional_auth_headers(),
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise NobitexError(f"margin/markets/list failed: {data}")
        markets = data.get("markets") or {}
        normalized: dict = {}
        for key, meta in markets.items():
            sym = self._margin_key_to_symbol(key, meta)
            normalized[sym] = {**(meta or {}), "_raw_key": key}
        self._margin_markets_cache = normalized
        self._margin_markets_cache_ts = now
        return normalized

    @staticmethod
    def _margin_key_to_symbol(key: str, meta: dict | None) -> str:
        k = (key or "").upper().replace("-", "").replace("_", "")
        if k.endswith("RLS"):
            k = k[:-3] + "IRT"
        if meta:
            src = str(meta.get("srcCurrency") or "").upper()
            dst = str(meta.get("dstCurrency") or "").lower()
            if src and dst:
                quote = "IRT" if dst in ("rls", "irt") else dst.upper()
                return f"{src}{quote}"
        return k

    async def get_market_stats(self, src_currency: str | None = None, dst_currency: str | None = None) -> dict:
        await throttle("market_stats")
        params = {}
        if src_currency:
            params["srcCurrency"] = src_currency
        if dst_currency:
            params["dstCurrency"] = dst_currency
        r = await self._client.get("/market/stats", params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise NobitexError(f"market/stats failed: {data}")
        return data.get("stats", {})

    async def get_liquid_symbols(self, top_n: int, quote: str = "IRT") -> list[str]:
        quote_u = quote.upper()
        margin = await self.get_margin_markets(details=True)

        candidates: list[str] = []
        for sym, meta in margin.items():
            if quote_u in ("IRT", "RLS"):
                if not (sym.endswith("IRT") or sym.endswith("RLS")):
                    continue
            elif not sym.endswith(quote_u):
                continue
            if meta.get("buyEnabled", True) is False or meta.get("sellEnabled", True) is False:
                continue
            candidates.append(sym)

        if not candidates:
            for sym, meta in margin.items():
                if quote_u in ("IRT", "RLS"):
                    if sym.endswith("IRT") or sym.endswith("RLS"):
                        candidates.append(sym)
                elif sym.endswith(quote_u):
                    candidates.append(sym)

        dst = "rls" if quote_u in ("IRT", "RLS") else quote.lower()
        try:
            stats = await self.get_market_stats(dst_currency=dst)
        except Exception:
            stats = {}

        vol_by_sym: dict[str, float] = {}
        for key, stat in (stats or {}).items():
            try:
                vol = float(stat.get("volumeDst") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            sym = self._stat_key_to_symbol(key, quote)
            vol_by_sym[sym] = vol

        ranked = sorted(candidates, key=lambda s: vol_by_sym.get(s, 0.0), reverse=True)
        return ranked[: max(1, int(top_n))]

    @staticmethod
    def _stat_key_to_symbol(stat_key: str, quote: str) -> str:
        base = stat_key.split("-")[0] if "-" in stat_key else stat_key[: -3]
        quote_symbol = "IRT" if quote.upper() in ("IRT", "RLS") else quote.upper()
        return f"{base.upper()}{quote_symbol}"

    async def get_ohlc(self, symbol: str, resolution: str, bars: int) -> dict:
        seconds_per_bar = self._resolution_seconds(resolution)
        to_ts = int(time.time())
        from_ts = to_ts - seconds_per_bar * max(bars, 1)

        rows: list[tuple] = []
        seen_t: set[int] = set()
        page = 1
        max_pages = max(2, (bars // 400) + 3)

        while page <= max_pages:
            await throttle("udf_history")
            params = {
                "symbol": symbol,
                "resolution": resolution,
                "from": from_ts,
                "to": to_ts,
                "page": page,
            }
            if page == 1 and bars <= 500:
                params["countback"] = bars

            r = await self._client.get("/market/udf/history", params=params)
            r.raise_for_status()
            data = r.json()
            if data.get("s") != "ok":
                break

            ts_list = data.get("t") or []
            if not ts_list:
                break

            o = data.get("o") or [None] * len(ts_list)
            h = data.get("h") or [None] * len(ts_list)
            l = data.get("l") or [None] * len(ts_list)
            c = data.get("c") or [None] * len(ts_list)
            v = data.get("v") or [None] * len(ts_list)

            new_count = 0
            for i, t in enumerate(ts_list):
                ti = int(t)
                if ti in seen_t:
                    continue
                seen_t.add(ti)
                rows.append((ti, o[i], h[i], l[i], c[i], v[i]))
                new_count += 1

            if len(ts_list) < 500 or new_count == 0:
                break
            page += 1

        rows.sort(key=lambda r: r[0])
        if len(rows) > bars:
            rows = rows[-bars:]

        out = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        for ti, oi, hi, li, ci, vi in rows:
            out["t"].append(ti)
            out["o"].append(oi)
            out["h"].append(hi)
            out["l"].append(li)
            out["c"].append(ci)
            out["v"].append(vi)
        return out

    @staticmethod
    def _resolution_seconds(resolution: str) -> int:
        if resolution.upper().endswith("D"):
            days = int(resolution[:-1]) if resolution[:-1].isdigit() else 1
            return days * 86400
        return int(resolution) * 60

    async def get_last_price(self, symbol: str) -> float:
        base = symbol[:-3] if not symbol.upper().endswith("USDT") else symbol[:-4]
        quote = symbol[len(base):]
        dst = "rls" if quote.upper() in ("IRT", "RLS") else quote.lower()
        stats = await self.get_market_stats(src_currency=base.lower(), dst_currency=dst)
        if not stats:
            raise NobitexError(f"No stats returned for {symbol}")
        stat = next(iter(stats.values()))
        return float(stat.get("latest") or stat.get("mark") or 0.0)

    def _split_symbol(self, symbol: str) -> tuple[str, str]:
        s = symbol.upper()
        if s.endswith("USDT"):
            return s[:-4].lower(), "usdt"
        if s.endswith("IRT") or s.endswith("RLS"):
            return s[:-3].lower(), "rls"
        return s[:-3].lower(), s[-3:].lower()

    # --------------------------------------------------------------- private / live

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        execution: str | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
        leverage: str = "1",
    ) -> dict:
        """
        POST /margin/orders/add — open a margin position leg (leverage default 1).
        Enforces min IRT notional when a price is known.
        """
        src, dst = self._split_symbol(symbol)
        exec_type = execution or ("market" if price is None else "limit")

        # Min order value guard for IRT
        if dst == "rls" and price is not None:
            notional = float(amount) * float(price)
            if notional < MIN_ORDER_VALUE_IRT:
                raise NobitexError(
                    f"Order notional {notional:.0f} IRT for {symbol} is below "
                    f"minimum {MIN_ORDER_VALUE_IRT:.0f} IRT"
                )

        payload = {
            "type": side,
            "execution": exec_type,
            "srcCurrency": src,
            "dstCurrency": dst,
            "amount": str(amount),
            "leverage": str(leverage),
        }
        if price is not None:
            payload["price"] = str(price)
        if stop_price is not None:
            payload["stopPrice"] = str(stop_price)
        if client_order_id:
            payload["clientOrderId"] = client_order_id

        await throttle("margin_orders_add")
        r = await self._client.post(
            "/margin/orders/add",
            json=payload,
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise NobitexError(f"margin order failed: {data}")
        return data

    async def list_positions(
        self,
        status: str = "active",
        src_currency: str | None = None,
        dst_currency: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> list[dict]:
        """GET /positions/list"""
        await throttle("positions_list")
        params = {"status": status, "page": page, "pageSize": page_size}
        if src_currency:
            params["srcCurrency"] = src_currency.lower()
        if dst_currency:
            params["dstCurrency"] = dst_currency.lower()
        r = await self._client.get(
            "/positions/list",
            params=params,
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise NobitexError(f"positions/list failed: {data}")
        return list(data.get("positions") or [])

    async def get_position(self, position_id: int) -> dict:
        await throttle("positions_status")
        r = await self._client.get(
            f"/positions/{int(position_id)}/status",
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise NobitexError(f"position status failed: {data}")
        return data.get("position") or {}

    async def close_position(
        self,
        position_id: int,
        amount: float,
        execution: str = "market",
        price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """
        POST /positions/{id}/close — proper margin close (opposite order).
        Prefer this over opening a new opposite margin order.
        """
        payload: dict = {
            "execution": execution,
            "amount": str(amount),
        }
        if execution == "limit" and price is not None:
            payload["price"] = str(price)
        if client_order_id:
            payload["clientOrderId"] = client_order_id

        await throttle("positions_close")
        r = await self._client.post(
            f"/positions/{int(position_id)}/close",
            json=payload,
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise NobitexError(f"position close failed: {data}")
        return data

    async def resolve_position_id(
        self,
        symbol: str,
        side: str,
        opened_after_iso: str | None = None,
    ) -> int | None:
        """
        Best-effort match of an open position for (symbol, side).
        side here is the *position* side (buy/sell), same as order type that opened it.
        """
        src, dst = self._split_symbol(symbol)
        positions = await self.list_positions(status="active", src_currency=src, dst_currency=dst)
        candidates = [
            p for p in positions
            if str(p.get("side", "")).lower() == side.lower()
            and str(p.get("status", "")).lower() in ("open", "active")
        ]
        if not candidates:
            return None
        # Prefer newest
        candidates.sort(key=lambda p: p.get("openedAt") or p.get("createdAt") or "", reverse=True)
        return int(candidates[0]["id"])

    async def transfer_wallet(self, currency: str, amount: float, src: str, dst: str) -> dict:
        await throttle("wallets_transfer")
        payload = {
            "currency": currency.lower(),
            "amount": str(amount),
            "src": src,
            "dst": dst,
        }
        r = await self._client.post(
            "/wallets/transfer",
            json=payload,
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise NobitexError(f"wallet transfer failed: {data}")
        return data


def sign_ed25519(private_key_pem: str, message: str) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(message.encode())
    return base64.urlsafe_b64encode(signature).decode()
