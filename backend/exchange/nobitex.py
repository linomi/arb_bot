"""
Live Nobitex REST client, built from the OpenAPI docs supplied with this
project (spot_trade.yaml, market_data.yaml).

IMPORTANT: this client is NOT exercised against the real API from this
sandbox (no outbound network access here) -- it is written directly from
the spec and must be smoke-tested against the Nobitex TESTNET
(https://testnetapiv2.nobitex.ir) before ever pointing it at mainnet.

Endpoints used:
  Public (no auth):
    GET  /market/stats                 -> 24h stats per market (for liquidity ranking)
    GET  /market/udf/history           -> OHLC candles
  Private (TokenAuth: "Authorization: Token <token>"):
    POST /market/orders/add            -> place order
    GET  /market/orders/status         -> order status
    GET  /market/orders/list           -> list orders
    POST /market/orders/update-status  -> cancel order
    GET  /market/trades/list           -> user's trade history

Auth: the docs define TWO private auth schemes:
  1. TokenAuth        -- header "Authorization: Token <token>" (implemented here)
  2. KeyAuth+Signature -- "Nobitex-Key" + Ed25519 "Nobitex-Signature" +
     "Nobitex-Timestamp" headers. Scaffolded below (sign_ed25519) but not
     wired to order placement -- wire it up if your account requires it.
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
        # Nobitex docs recommend TraderBot/<name> User-Agent for bots.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "TraderBot/StatArb-1.0"},
        )

    async def aclose(self):
        await self._client.aclose()

    def _auth_headers(self) -> dict:
        if not self.token:
            raise NobitexError("No API token configured for live trading. Add one in Settings -> Credentials.")
        return {"Authorization": f"Token {self.token}"}

    # ---------------------------------------------------------------- public

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
        Rank markets by 24h quote-currency volume (volumeDst) and return the
        top-N symbols (e.g. "BTCIRT"). `quote` should match how you want to
        express `dstCurrency` in the udf/history `symbol` field (Nobitex
        symbols look like BTCIRT / BTCUSDT).
        """
        dst = "rls" if quote.upper() in ("IRT", "RLS") else quote.lower()
        stats = await self.get_market_stats(dst_currency=dst)
        ranked = []
        for key, stat in stats.items():
            try:
                vol = float(stat.get("volumeDst") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            ranked.append((key, vol))
        ranked.sort(key=lambda kv: kv[1], reverse=True)
        top_keys = [k for k, _ in ranked[:top_n]]
        return [self._stat_key_to_symbol(k, quote) for k in top_keys]

    @staticmethod
    def _stat_key_to_symbol(stat_key: str, quote: str) -> str:
        # stats keys are like "btc-rls" or "btcrls" depending on API version;
        # normalize both into a udf/history-style symbol e.g. "BTCIRT".
        base = stat_key.split("-")[0] if "-" in stat_key else stat_key[: -3]
        quote_symbol = "IRT" if quote.upper() in ("IRT", "RLS") else quote.upper()
        return f"{base.upper()}{quote_symbol}"

    async def get_ohlc(self, symbol: str, resolution: str, bars: int) -> dict:
        """
        resolution: one of "1","5","15","30","60","180","240","360","720","D","1D","2D","3D"
        bars: how many candles you want back (this method pages if needed,
        since the API caps a single response at 500 candles).

        Pagination strategy (per Nobitex UDF docs):
        - Keep a fixed `to` (now).
        - Request with increasing `page` to walk older batches.
        - Also pass `from` as the overall lower bound.
        - Collect, deduplicate by timestamp, sort ascending, trim to `bars`.
        """
        seconds_per_bar = self._resolution_seconds(resolution)
        to_ts = int(time.time())
        from_ts = to_ts - seconds_per_bar * max(bars, 1)

        # Collect as list of (t, o, h, l, c, v) to make dedup easy.
        rows: list[tuple] = []
        seen_t: set[int] = set()
        page = 1
        max_pages = max(2, (bars // 400) + 3)  # safety cap

        while page <= max_pages:
            params = {
                "symbol": symbol,
                "resolution": resolution,
                "from": from_ts,
                "to": to_ts,
                "page": page,
            }
            # Prefer countback on first page when possible (docs: countback
            # overrides from and is capped at 500).
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

        # Sort ascending by time and keep the most recent `bars`.
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
    ) -> dict:
        base = symbol[:-3]
        quote = symbol[-3:]
        dst = "rls" if quote.upper() in ("IRT", "RLS") else quote.lower()
        payload = {
            "type": side,
            "execution": execution or ("market" if price is None else "limit"),
            "srcCurrency": base.lower(),
            "dstCurrency": dst,
            "amount": str(amount),
        }
        if price is not None:
            payload["price"] = str(price)
        if stop_price is not None:
            payload["stopPrice"] = str(stop_price)
        if client_order_id:
            payload["clientOrderId"] = client_order_id

        r = await self._client.post("/market/orders/add", json=payload, headers=self._auth_headers())
        r.raise_for_status()
        return r.json()

    async def get_order_status(self, order_id: int) -> dict:
        r = await self._client.get("/market/orders/status", params={"id": order_id}, headers=self._auth_headers())
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


def sign_ed25519(private_key_pem: str, message: str) -> str:
    """
    Scaffold for the KeyAuth+SignatureAuth flow (Ed25519 signature, URL-safe
    base64). Not wired into NobitexClient by default -- TokenAuth is simpler
    and sufficient for most accounts. Enable this path only if your API key
    requires signature auth; consult the current Nobitex docs for the exact
    message-to-sign format (typically method+path+timestamp+body) since it
    is not fully specified in the bundled OpenAPI file.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(message.encode())
    return base64.urlsafe_b64encode(signature).decode()
