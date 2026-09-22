"""GitHub-Codespace-style random names for trading groups."""
from __future__ import annotations

import random

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
    "basis", "carry", "roll", "spot", "perp", "margin", "notional",
    "residual", "zscore", "station", "cointeg", "factor", "leg",
    "wizard", "oracle", "sentinel", "ranger", "pilot", "engine",
    "forge", "anvil", "compass", "lighthouse", "harbor", "ridge",
]


def generate_group_name(rng: random.Random | None = None, used: set[str] | None = None) -> str:
    """Return adjective_noun, optionally unique against `used`."""
    r = rng or random.Random()
    used = used or set()
    for _ in range(80):
        name = f"{r.choice(_ADJECTIVES)}_{r.choice(_NOUNS)}"
        if name not in used:
            return name
    return f"{r.choice(_ADJECTIVES)}_{r.choice(_NOUNS)}_{r.randint(10, 99)}"
