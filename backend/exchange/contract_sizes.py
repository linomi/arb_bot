"""Persistent cache of XT contract sizes (base units per contract), keyed by unified symbol.

XT perpetuals trade in WHOLE contracts (1 XRP contract = 10 XRP, 1 QNT contract = 0.1 QNT ...), so the
sizing code needs each symbol's contract size to round legs the way the exchange will. The live client
fills this cache every time it loads XT markets; paper mode and the backtester read it without any
network access. A symbol that is not cached simply is not rounded (treated as continuous).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from backend.db import DATA_DIR

log = logging.getLogger("contract_sizes")

_PATH: Path = DATA_DIR / "xt_contract_sizes.json"
_LOCK = threading.Lock()
_CACHE: dict[str, float] | None = None


def _load() -> dict[str, float]:
    global _CACHE
    if _CACHE is None:
        try:
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            _CACHE = {str(k): float(v) for k, v in raw.items() if float(v) > 0}
        except (OSError, ValueError, TypeError):
            _CACHE = {}
    return _CACHE


def reset_for_tests(path: Path | None = None) -> None:
    global _CACHE, _PATH
    with _LOCK:
        _CACHE = None
        if path is not None:
            _PATH = Path(path)


def update(mapping: dict[str, float]) -> None:
    """Merge ``mapping`` into the cache and persist if anything changed."""
    clean = {}
    for k, v in (mapping or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            clean[str(k)] = f
    if not clean:
        return
    with _LOCK:
        cur = _load()
        if all(cur.get(k) == v for k, v in clean.items()):
            return
        cur.update(clean)
        try:
            _PATH.write_text(json.dumps(cur, sort_keys=True), encoding="utf-8")
        except OSError as e:
            log.debug("could not persist contract sizes: %s", e)


def update_from_markets(markets: dict | None) -> None:
    out: dict[str, float] = {}
    for sym, m in (markets or {}).items():
        try:
            if not (m.get("swap") and m.get("linear")):
                continue
            cs = m.get("contractSize")
            if cs is None and isinstance(m.get("info"), dict):
                cs = m["info"].get("contractSize")
            if cs is not None and float(cs) > 0:
                out[str(sym)] = float(cs)
        except Exception:
            continue
    update(out)


def lookup(symbol: str) -> float | None:
    with _LOCK:
        return _load().get(str(symbol))


def lookup_many(symbols) -> dict[str, float]:
    with _LOCK:
        cur = _load()
        return {s: cur[s] for s in symbols if s in cur}
