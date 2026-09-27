"""
Live Nobitex REST client — margin markets + margin orders + positions.

Auth modes:
  1) Token:  Authorization: Token <token>
  2) API Key (Ed25519): Nobitex-Key / Nobitex-Signature / Nobitex-Timestamp

Rate limits enforced via backend.exchange.rate_limit.
"""
from __future__ import annotations

import time
import base64
import json
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from backend.exchange.rate_limit import throttle
from backend.strategy.sizing import MIN_ORDER_VALUE_IRT

BASE_URL_DEFAULT = "https://apiv2.nobitex.ir"

# Nobitex public market-data endpoints (/market/udf/history, /market/stats) return
# Rial-market prices in Toman (1 Toman = 10 Rial). Margin/position/order endpoints
# use Rial. Multiply market-data prices by this factor so the rest of the bot
# (OLS, z-score, sizing, MIN_ORDER_VALUE_IRT, PnL) sees a single Rial unit.
# Confirmed empirically: position entryPrice / OHLC-or-stats price ≈ 10.0 across
# many symbols. Official docs claim Rial everywhere, but the live APIs diverge.
NOBITEX_MARKET_DATA_SCALE = 10.0


class NobitexError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def _load_ed25519_private(secret: str) -> Ed25519PrivateKey:
    s = (secret or "").strip()
    if not s:
        raise NobitexError("Empty private key")

    if "BEGIN" in s and "PRIVATE KEY" in s:
        key = load_pem_private_key(s.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise NobitexError("PEM private key is not Ed25519")
        return key

    hx = s.lower().replace("0x", "")
    if all(c in "0123456789abcdef" for c in hx) and len(hx) in (64, 128):
        raw = bytes.fromhex(hx[:64])
        return Ed25519PrivateKey.from_private_bytes(raw)

    pad = "=" * ((4 - len(s) % 4) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(s + pad)
            if len(raw) == 32:
                return Ed25519PrivateKey.from_private_bytes(raw)
            if len(raw) == 64:
                return Ed25519PrivateKey.from_private_bytes(raw[:32])
        except Exception:
            continue

    raise NobitexError(
        "Could not parse private key. Paste the Nobitex secretKey (base64) "
        "or a PEM Ed25519 private key."
    )
