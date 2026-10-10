"""Telegram transport: long-polling, allow-list, throttled sending, daily digest.

Configuration (environment / .env):
  TELEGRAM_BOT_TOKEN   token from @BotFather (required to enable)
  TELEGRAM_CHAT_IDS    comma/space separated numeric user ids allowed to talk to the bot
                       and receiving notifications (required to enable)
  TELEGRAM_PIN         optional; must accompany /confirm for sensitive actions
  TELEGRAM_PROXY       optional proxy URL (http://host:port) if Telegram is blocked
  TELEGRAM_DIGEST_TIME HH:MM local time of the daily digest (default 21:00, "off" disables)
  TELEGRAM_TZ          default Asia/Tehran
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from typing import Any

import httpx

from backend.notify import fa
from backend.notify.commands import CommandHandler, LogRing, Reply
from backend.notify.hub import hub

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_AGE_SEC = 120
QUEUE_MAX = 200
SEND_GAP_SEC = 0.4


def parse_chat_ids(raw: str | None) -> set[int]:
    out: set[int] = set()
    for tok in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if tok.lstrip("-").isdigit():
            out.add(int(tok))
    return out


def from_env() -> "TelegramBot | None":
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    ids = parse_chat_ids(os.environ.get("TELEGRAM_CHAT_IDS"))
    if not token:
        return None
    if not ids:
        log.warning("TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_IDS is empty — Telegram stays disabled "
                    "(the allow-list is mandatory).")
        return None
    return TelegramBot(
        token=token, chat_ids=ids,
        pin=os.environ.get("TELEGRAM_PIN"),
        proxy=(os.environ.get("TELEGRAM_PROXY") or "").strip() or None,
        digest_time=(os.environ.get("TELEGRAM_DIGEST_TIME") or "21:00").strip(),
        tz=(os.environ.get("TELEGRAM_TZ") or "Asia/Tehran").strip(),
        api_base=(os.environ.get("TELEGRAM_API_BASE") or API_BASE).rstrip("/"),
    )


class TelegramBot:
    def __init__(self, *, token: str, chat_ids: set[int], pin: str | None = None, proxy: str | None = None,
                 digest_time: str = "21:00", tz: str = "Asia/Tehran", api_base: str = API_BASE,
                 handler: CommandHandler | None = None, http: httpx.AsyncClient | None = None):
        self.token = token
        self.chat_ids = set(chat_ids)
        self.tz = tz
        self.digest_time = digest_time
        self.api_base = api_base
        self.log_ring = LogRing()
        self.handler = handler or CommandHandler(pin=pin, tz=tz, log_ring=self.log_ring)
        self._http = http
        self._owns_http = http is None
        self._proxy = proxy
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._offset: int | None = None
        self._last_sent: dict[str, float] = {}
        self.connected = False
        self.last_error: str | None = None
        self.started = False
        self._pending = 0  # messages queued or being sent (used to flush on shutdown)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.started:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if self._http is None:
            kw: dict[str, Any] = {"timeout": httpx.Timeout(40.0, connect=15.0)}
            if self._proxy:
                kw["proxy"] = self._proxy
            self._http = httpx.AsyncClient(**kw)
        logging.getLogger().addHandler(self.log_ring)
        hub.add_sink(self._sink)
        self.started = True
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="tg-poll"),
            asyncio.create_task(self._send_loop(), name="tg-send"),
        ]
        if self.digest_time and self.digest_time.lower() != "off":
            self._tasks.append(asyncio.create_task(self._digest_loop(), name="tg-digest"))
        log.info("Telegram bot enabled for %d chat(s)", len(self.chat_ids))

    async def stop(self, flush_timeout: float = 4.0) -> None:
        if not self.started:
            return
        hub.remove_sink(self._sink)
        # best-effort flush of whatever is queued (e.g. the shutdown notice)
        if self._queue is not None:
            await asyncio.sleep(0)  # let call_soon_threadsafe puts run
            deadline = time.monotonic() + flush_timeout
            while self._pending > 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        logging.getLogger().removeHandler(self.log_ring)
        if self._http is not None and self._owns_http:
            await self._http.aclose()
        self.started = False

    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "last_error": self.last_error,
                "chats": len(self.chat_ids)}

    # ------------------------------------------------------------------ API
    async def api(self, method: str, *, json: dict | None = None, files: dict | None = None,
                  data: dict | None = None, timeout: float | None = None) -> Any:
        url = f"{self.api_base}/bot{self.token}/{method}"
        kw: dict[str, Any] = {}
        if timeout:
            kw["timeout"] = timeout
        if files:
            r = await self._http.post(url, data=data, files=files, **kw)
        else:
            r = await self._http.post(url, json=json or {}, **kw)
        try:
            body = r.json()
        except Exception:
            r.raise_for_status()
            raise
        if not body.get("ok"):
            err = TelegramError(body.get("description") or f"HTTP {r.status_code}", body.get("error_code") or r.status_code,
                                (body.get("parameters") or {}).get("retry_after"))
            raise err
        return body.get("result")

    # ------------------------------------------------------------------ receiving
    def _authorized(self, upd_msg: dict) -> bool:
        chat = (upd_msg.get("chat") or {}).get("id")
        user = (upd_msg.get("from") or {}).get("id")
        return chat in self.chat_ids and user in self.chat_ids

    async def handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg or "text" not in msg:
            return
        if not self._authorized(msg):
            log.warning("ignored message from unauthorized chat/user: chat=%s user=%s",
                        (msg.get("chat") or {}).get("id"), (msg.get("from") or {}).get("id"))
            return
        if time.time() - float(msg.get("date") or 0) > MAX_MESSAGE_AGE_SEC:
            return  # stale (e.g. delivered after an outage): never replay commands
        chat_id = msg["chat"]["id"]
        replies = await self.handler.handle(chat_id, msg["text"])
        for r in replies:
            await self._reply(chat_id, r)

    async def _reply(self, chat_id: int, r: Reply) -> None:
        try:
            if r.photo:
                await self.api("sendPhoto", data={"chat_id": str(chat_id), "caption": r.text[:1000], "parse_mode": "HTML"},
                               files={"photo": (r.photo_name, r.photo, "image/png")})
            else:
                await self._send_text(chat_id, r.text)
        except Exception as e:
            log.warning("reply failed: %s", e)

    async def _send_text(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text):
            try:
                await self.api("sendMessage", json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                                                    "disable_web_page_preview": True})
            except TelegramError as e:
                if e.code == 400 and "parse" in str(e).lower():
                    await self.api("sendMessage", json={"chat_id": chat_id, "text": chunk})
                else:
                    raise

    async def _poll_loop(self) -> None:
        backoff = 3.0
        # Drop anything that piled up while the bot was down: commands must not replay.
        try:
            pending = await self.api("getUpdates", json={"offset": -1, "timeout": 0})
            if pending:
                self._offset = pending[-1]["update_id"] + 1
            self.connected = True
        except Exception as e:
            self.last_error = str(e)
        while True:
            try:
                params: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "edited_message"]}
                if self._offset is not None:
                    params["offset"] = self._offset
                updates = await self.api("getUpdates", json=params, timeout=40.0)
                self.connected = True
                self.last_error = None
                backoff = 3.0
                for u in updates or []:
                    self._offset = u["update_id"] + 1
                    try:
                        await self.handle_update(u)
                    except Exception:
                        log.exception("update handling failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self.last_error = str(e)[:200]
                log.warning("telegram poll error: %s (retry in %.0fs)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    # ------------------------------------------------------------------ sending
    def _sink(self, kind: str, data: dict) -> None:
        text = fa.format_event(kind, data)
        if not text:
            return
        window = fa.SUPPRESS_SEC.get(kind)
        if window:
            key = f"{kind}:{data.get('trade_id') or data.get('group') or ''}:{str(data.get('error') or '')[:60]}"
            now = time.monotonic()
            if now - self._last_sent.get(key, -1e9) < window:
                return
            self._last_sent[key] = now
        self.enqueue(text)

    def enqueue(self, text: str, chat_ids: set[int] | None = None) -> None:
        loop, q = self._loop, self._queue
        if loop is None or q is None:
            return

        def put():
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest rather than blocking trading code
                    self._pending -= 1
                except Exception:
                    pass
            self._pending += 1
            q.put_nowait((text, chat_ids))

        loop.call_soon_threadsafe(put)

    async def _send_loop(self) -> None:
        assert self._queue is not None
        while True:
            text, chat_ids = await self._queue.get()
            for cid in sorted(chat_ids or self.chat_ids):
                for attempt in range(3):
                    try:
                        await self._send_text(cid, text)
                        break
                    except asyncio.CancelledError:
                        raise
                    except TelegramError as e:
                        if e.code == 429:
                            await asyncio.sleep(float(e.retry_after or 5) + 0.5)
                            continue
                        log.warning("send failed (chat %s): %s", cid, e)
                        break
                    except Exception as e:
                        self.last_error = str(e)[:200]
                        await asyncio.sleep(2.0 * (attempt + 1))
                await asyncio.sleep(SEND_GAP_SEC)
            self._pending = max(0, self._pending - 1)

    # ------------------------------------------------------------------ digest
    def _next_digest_delay(self, now: dt.datetime | None = None) -> float:
        from zoneinfo import ZoneInfo
        try:
            hh, mm = (int(x) for x in self.digest_time.split(":"))
        except Exception:
            hh, mm = 21, 0
        tz = ZoneInfo(self.tz)
        now = now or dt.datetime.now(tz)
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        return (target - now).total_seconds()

    async def _digest_loop(self) -> None:
        while True:
            await asyncio.sleep(self._next_digest_delay())
            try:
                self.enqueue(await self.handler.build_digest())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("digest failed")
            await asyncio.sleep(61)  # never fire twice for the same minute


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        out.append(cur)
    return out


# singleton used by main.py
bot: TelegramBot | None = None
