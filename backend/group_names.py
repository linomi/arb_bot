"""GitHub-Codespace-style random names for trading groups."""
from __future__ import annotations

import random
import re

_ADJECTIVES = [
    "alpha", "beta", "gamma", "delta", "sigma", "theta", "omega",
    "silent", "swift", "liquid", "solid", "volatile", "steady", "keen",
    "sharp", "quiet", "bold", "calm", "bright", "dark", "golden",
    "silver", "iron", "atomic", "quantum", "latent", "mean", "lean",
    "prime", "nested", "paired", "hedged", "neutral", "long", "short",
    "deep", "shallow", "fast", "slow", "dense", "sparse", "clean",
]

_NOUNS = [
    "spread", "basis", "arb", "hedge", "pair", "basket", "book",
    "ledger", "vault", "node", "relay", "signal", "pulse", "tick",
    "candle", "order", "fill", "lot", "clip", "edge", "alpha",
    "beta", "gamma", "delta", "theta", "vega", "rho", "skew",
    "carry", "roll", "spot", "perp", "margin", "notional",
    "residual", "zscore", "station", "cointeg", "factor", "leg",
    "wizard", "oracle", "sentinel", "ranger", "pilot", "engine",
    "forge", "anvil", "compass", "lighthouse", "harbor", "ridge",
]

_ADJ_SET = set(_ADJECTIVES)
_NOUN_SET = set(_NOUNS)


def generate_group_name(rng: random.Random | None = None, used: set[str] | None = None) -> str:
    """Return adjective_noun, optionally unique against `used`."""
    r = rng or random.Random()
    used = used or set()
    for _ in range(80):
        name = f"{r.choice(_ADJECTIVES)}_{r.choice(_NOUNS)}"
        if name not in used:
            return name
    return f"{r.choice(_ADJECTIVES)}_{r.choice(_NOUNS)}_{r.randint(10, 99)}"


def is_codename(name: str | None) -> bool:
    """True if name already looks like adjective_noun (or with trailing _NN)."""
    if not name or not isinstance(name, str):
        return False
    m = re.fullmatch(r"([a-z]+)_([a-z]+)(?:_\d{1,3})?", name.strip().lower())
    if not m:
        return False
    return m.group(1) in _ADJ_SET and m.group(2) in _NOUN_SET


def is_legacy_name(name: str | None) -> bool:
    """Names produced by older init (random-BTCIRT-…, manual-…, raw symbol lists)."""
    if not name or not str(name).strip():
        return True
    n = str(name).strip()
    if is_codename(n):
        return False
    low = n.lower()
    if low.startswith(("random-", "sector-", "manual-")):
        return True
    if re.search(r"(IRT|USDT|RLS)\b", n, re.I):
        return True
    if n.count("-") >= 2:
        return True
    return False
