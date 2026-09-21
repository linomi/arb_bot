"""
Live Nobitex REST client.

Market *universe* comes from margin (leverage) markets, not the full spot list:
  GET  /margin/markets/list          -> margin-enabled pairs
  GET  /market/stats                 -> volume ranking (filtered to margin pairs)
  GET  /market/udf/history           -> OHLC (same underlying symbols)

Orders (live) go through margin API so shorts/longs are both possible:
  POST /margin/orders/add            -> margin order, default leverage "1"
  GET  /market/orders/status         -> order status (shared)
  POST /market/orders/update-status  -> cancel

Auth: Authorization: Token <token>
"""
import time
import base64
import httpx

BASE_URL_DEFAULT = "https://apiv2.nobitex.ir"


class NobitexError(RuntimeError):
    pass


class NobitexClient:
    def __init__(self, base_url: str = BASE_URL_DEFAULT, token: str | None = None, timeout: float = 15.0):
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
        """
        GET /margin/markets/list — markets that support margin/leverage trading.

        Returns dict keyed by market symbol e.g. {"BTCIRT": {...}, "ETHUSDT": {...}}.
        Cached ~60s to stay under the 30 req/min limit.
        """
        now = time.time()
        if (
            not force
            and self._margin_markets_cache is not None
            and (now - self._margin_markets_cache_ts) < 60
        ):
            return self._margin_markets_cache

        # Spec requires JSON body even on GET (details flag).
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
        # Normalize keys to our internal symbol form (BTCIRT not BTCUSDT quirks).
        normalized: dict = {}
        for key, meta in markets.items():
            sym = self._margin_key_to_symbol(key, meta)
            normalized[sym] = {**(meta or {}), "_raw_key": key}
        self._margin_markets_cache = normalized
        self._margin_markets_cache_ts = now
        return normalized

    @staticmethod
    def _margin_key_to_symbol(key: str, meta: dict | None) -> str:
        """BTCUSDT / BTCIRT style keys from margin API -> BTCUSDT / BTCIRT."""
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
        """
        Top-N symbols by 24h volume, **restricted to margin-enabled markets**
        that allow both buy and sell positions when possible.
        """
        quote_u = quote.upper()
        margin = await self.get_margin_markets(details=True)

        # Keep only markets matching quote and with trading enabled.
        candidates: list[str] = []
        for sym, meta in margin.items():
            if not sym.endswith(quote_u) and not (
                quote_u in ("IRT", "RLS") and (sym.endswith("IRT") or sym.endswith("RLS"))
            ):
                # loose match: if quote is IRT, accept *IRT
                if quote_u in ("IRT", "RLS"):
                    if not (sym.endswith("IRT") or sym.endswith("RLS")):
                        continue
                else:
                    if not sym.endswith(quote_u):
                        continue
            buy_ok = meta.get("buyEnabled", True)
            sell_ok = meta.get("sellEnabled", True)
            # Need both sides for residual hedges (long and short legs).
            if buy_ok is False or sell_ok is False:
                continue
            candidates.append(sym)

        if not candidates:
            # Fall back: any margin market for this quote even if one side disabled
            for sym, meta in margin.items():
                if quote_u in ("IRT", "RLS"):
                    if sym.endswith("IRT") or sym.endswith("RLS"):
                        candidates.append(sym)
                elif sym.endswith(quote_u):
                    candidates.append(sym)

        # Rank by spot 24h volume among margin candidates
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

        ranked = sorted(
            candidates,
            key=lambda s: vol_by_sym.get(s, 0.0),
            reverse=True,
        )
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
        base = symbol[:-3]
        quote = symbol[-3:]
        dst = "rls" if quote.upper() in ("IRT", "RLS") else quote.lower()
        stats = await self.get_market_stats(src_currency=base.lower(), dst_currency=dst)
        if not stats:
            raise NobitexError(f"No stats returned for {symbol}")
        stat = next(iter(stats.values()))
        return float(stat.get("latest") or stat.get("mark") or 0.0)

    def _split_symbol(self, symbol: str) -> tuple[str, str]:
        """BTCIRT -> (btc, rls); BTCUSDT -> (btc, usdt)."""
        s = symbol.upper()
        if s.endswith("USDT"):
            return s[:-4].lower(), "usdt"
        if s.endswith("IRT") or s.endswith("RLS"):
            return s[:-3].lower(), "rls"
        # fallback: last 3 chars as quote
        return s[:-3].lower(), s[-3:].lower()

    # --------------------------------------------------------------- private

    async def place_order(
        self,
        symbol: str,
        side: str,               # "buy" | "sell"
        amount: float,
        price: float | None = None,
        execution: str | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
        leverage: str = "1",
    ) -> dict:
        """
        Place a **margin** order (POST /margin/orders/add).

        Default leverage is 1 (no extra gearing) — margin is used so both
        long and short residual legs are possible, not for amplification.
        """
        src, dst = self._split_symbol(symbol)
        payload = {
            "type": side,
            "execution": execution or ("market" if price is None else "limit"),
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

    async def get_order_status(self, order_id: int) -> dict:
        r = await self._client.get(
            "/market/orders/status",
            params={"id": order_id},
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        return r.json()

    async def cancel_order(self, order_id: int) -> dict:
        r = await self._client.post(
            "/market/orders/update-status",
            json={"order": order_id, "status": "canceled"},
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        return r.json()

    async def transfer_wallet(self, currency: str, amount: float, src: str, dst: str) -> dict:
        """POST /wallets/transfer — e.g. spot -> margin before trading."""
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
