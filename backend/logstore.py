"""Searchable application log: a logging handler that keeps the last N records in memory and
mirrors them to a size-rotated JSONL file, so the Logs tab survives restarts.

Each record is {id, ts (UTC iso), level, logger, cat, group_id, msg}.
``cat`` (category) is derived from the logger name, plus a "trade" category for engine lines that
are about opening/closing/rolling back positions, so the UI can show "trade logs only"."""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import re
import threading
from pathlib import Path

CATEGORIES = ("trade", "engine", "exchange", "telegram", "init", "api", "other")

_SKIP_PREFIXES = ("uvicorn.access", "httpx", "httpcore", "backend.logstore")
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_TRADE_RE = re.compile(
    r"trade #\d+|\bopened\b|partial entry|entry order failed|entry blocked|entry preflight|"
    r"rolling back|rolled back|rollback|ORPHAN|exit place failed|time stop|risk[_ ]exit|close leg",
    re.I,
)
_GROUP_RE = re.compile(r"\bgroup (\d+)\b")


def categorize(logger_name: str, msg: str) -> str:
    n = logger_name or ""
    if n.startswith("telegram") or n == "notify":
        return "telegram"
    if n in ("xt", "nobitex") or n.startswith(("backend.exchange", "xt.", "nobitex")):
        return "exchange"
    if n in ("init_router",):
        return "init"
    if n in ("groups_router", "main", "db"):
        return "api"
    if n in ("bot_engine", "sizing"):
        return "trade" if _TRADE_RE.search(msg) else "engine"
    return "other"


class LogStore(logging.Handler):
    def __init__(self, path: Path | None = None, size: int = 5000, max_bytes: int = 5_000_000):
        super().__init__(level=logging.INFO)
        self.buf: collections.deque[dict] = collections.deque(maxlen=size)
        self._lock = threading.Lock()
        self._seq = 0
        self.path = path
        self.max_bytes = max_bytes
        self._fh = None
        if path is not None:
            self._load(path, size)
            try:
                self._fh = open(path, "a", encoding="utf-8")
            except OSError:
                self._fh = None

    # ------------------------------------------------------------------ persistence
    def _load(self, path: Path, size: int) -> None:
        rows: list[dict] = []
        for p in (path.with_name(path.name + ".1"), path):
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                continue
        for r in rows[-size:]:
            self.buf.append(r)
            self._seq = max(self._seq, int(r.get("id") or 0))

    def _rotate_if_needed(self) -> None:
        if self._fh is None or self.path is None:
            return
        try:
            if self._fh.tell() < self.max_bytes:
                return
            self._fh.close()
            self.path.replace(self.path.with_name(self.path.name + ".1"))
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError:
            self._fh = None

    # ------------------------------------------------------------------ handler
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_SKIP_PREFIXES):
                return
            msg = record.getMessage()
            if record.exc_info:
                msg += " | " + (logging.Formatter().formatException(record.exc_info).strip().splitlines() or [""])[-1]
            m = _GROUP_RE.search(msg)
            with self._lock:
                self._seq += 1
                row = {
                    "id": self._seq,
                    "ts": dt.datetime.utcfromtimestamp(record.created).isoformat(timespec="milliseconds") + "Z",
                    "level": record.levelname,
                    "logger": record.name,
                    "cat": categorize(record.name, msg),
                    "group_id": int(m.group(1)) if m else None,
                    "msg": msg[:2000],
                }
                self.buf.append(row)
                if self._fh is not None:
                    self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    self._fh.flush()
                    self._rotate_if_needed()
        except Exception:  # logging must never break the app
            pass

    # ------------------------------------------------------------------ query
    def query(self, *, level: str | None = None, category: str | None = None, q: str | None = None,
              group_id: int | None = None, before_id: int | None = None, after_id: int | None = None,
              limit: int = 200) -> dict:
        min_lvl = _LEVELS.get((level or "").upper(), 0)
        cats = {c for c in (category or "").lower().split(",") if c}
        words = [w for w in (q or "").lower().split() if w]
        with self._lock:
            rows = list(self.buf)
        out: list[dict] = []
        matched = 0
        for r in reversed(rows):  # newest first
            rid = r["id"]
            if before_id is not None and rid >= before_id:
                continue
            if after_id is not None and rid <= after_id:
                continue
            if min_lvl and _LEVELS.get(r["level"], 0) < min_lvl:
                continue
            if cats and r["cat"] not in cats:
                continue
            if group_id is not None and r.get("group_id") != group_id:
                continue
            if words:
                hay = f'{r["msg"]} {r["logger"]} {r["level"]}'.lower()
                if not all(w in hay for w in words):
                    continue
            matched += 1
            if len(out) < limit:
                out.append(r)
        return {"items": out, "matched": matched, "last_id": rows[-1]["id"] if rows else 0,
                "total": len(rows), "has_more": matched > len(out)}

    def close(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        finally:
            super().close()


store: LogStore | None = None


def install(path: Path | None = None) -> LogStore:
    """Attach the singleton store to the root logger (idempotent)."""
    global store
    if store is None:
        store = LogStore(path)
        logging.getLogger().addHandler(store)
    return store
