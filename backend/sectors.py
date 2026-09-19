"""
Heuristic sector map, used only by the "sector-based" initialization method.

This is NOT pulled live from anywhere (no reliable free/open API for
this exists that's stable enough to depend on) -- it's a maintained,
editable heuristic based on publicly known project categories. Edit this
dict freely; it is the entire "open source information" referenced in the
spec. Base currency symbol (before the market suffix, e.g. "BTC" from
"BTCIRT") is the lookup key, uppercase.

Add coins here as you add markets. Anything not listed falls into "OTHER".
"""

SECTOR_MAP: dict[str, str] = {
    # Layer 1 / smart-contract platforms
    "BTC": "STORE_OF_VALUE", "ETH": "L1", "SOL": "L1", "ADA": "L1", "AVAX": "L1",
    "DOT": "L1", "NEAR": "L1", "ATOM": "L1", "TON": "L1", "APT": "L1", "SUI": "L1",
    "TRX": "L1", "ETC": "L1", "ALGO": "L1", "EGLD": "L1", "HBAR": "L1", "SEI": "L1",
    "INJ": "L1",

    # Layer 2 / scaling
    "MATIC": "L2", "POL": "L2", "ARB": "L2", "OP": "L2", "IMX": "L2", "STRK": "L2",

    # DeFi
    "UNI": "DEFI", "AAVE": "DEFI", "MKR": "DEFI", "CRV": "DEFI", "COMP": "DEFI",
    "SNX": "DEFI", "SUSHI": "DEFI", "1INCH": "DEFI", "LDO": "DEFI", "DYDX": "DEFI",
    "CAKE": "DEFI", "GMX": "DEFI",

    # Oracles / infra
    "LINK": "ORACLE_INFRA", "GRT": "ORACLE_INFRA", "FIL": "ORACLE_INFRA",
    "AR": "ORACLE_INFRA", "RENDER": "ORACLE_INFRA", "RNDR": "ORACLE_INFRA",

    # Exchange tokens
    "BNB": "EXCHANGE_TOKEN", "OKB": "EXCHANGE_TOKEN", "CRO": "EXCHANGE_TOKEN",
    "LEO": "EXCHANGE_TOKEN",

    # Payments
    "XRP": "PAYMENTS", "XLM": "PAYMENTS", "LTC": "PAYMENTS", "BCH": "PAYMENTS",
    "DASH": "PAYMENTS", "XMR": "PRIVACY", "ZEC": "PRIVACY",

    # Meme
    "DOGE": "MEME", "SHIB": "MEME", "PEPE": "MEME", "FLOKI": "MEME", "WIF": "MEME",
    "BONK": "MEME",

    # Gaming / metaverse
    "SAND": "GAMING_METAVERSE", "MANA": "GAMING_METAVERSE", "AXS": "GAMING_METAVERSE",
    "GALA": "GAMING_METAVERSE", "ENJ": "GAMING_METAVERSE", "APE": "GAMING_METAVERSE",

    # AI narrative
    "FET": "AI", "AGIX": "AI", "OCEAN": "AI", "TAO": "AI", "WLD": "AI",
}


def sector_of(symbol: str, quote_suffixes: tuple[str, ...] = ("IRT", "RLS", "USDT", "USDC")) -> str:
    """
    Extract the base currency from a market symbol (e.g. "BTCIRT" -> "BTC")
    and return its sector. Falls back to "OTHER" if unmapped.
    """
    s = symbol.upper()
    base = s
    for suf in quote_suffixes:
        if s.endswith(suf) and len(s) > len(suf):
            base = s[: -len(suf)]
            break
    return SECTOR_MAP.get(base, "OTHER")


def group_symbols_by_sector(symbols: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    for sym in symbols:
        sec = sector_of(sym)
        buckets.setdefault(sec, []).append(sym)
    return buckets
