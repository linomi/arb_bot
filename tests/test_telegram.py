import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_service
from backend.models import Base, BotState, Group, OLSFit, Trade
from backend.notify import emit, fa
from backend.notify.commands import CommandHandler
from backend.notify.jalali import fmt_jalali, gregorian_to_jalali
from backend.notify.telegram import TelegramBot, from_env, parse_chat_ids


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def sf():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng)
    db = factory()
    config_service.seed_defaults_if_missing(db)
    db.close()
    return factory


def _engine(**kw):
    calls = []

    async def close_all(reason="manual_closeall"):
        calls.append(reason)
        return {"total": 2, "closed": 2, "failed": 0}

    return SimpleNamespace(_group_diag=kw.get("diag", {}), _orphan_positions=[], _last_error=None,
                           _pause_until=None, close_all=close_all, calls=calls)


def _seed(factory, *, mode="paper", open_trade=True):
    db = factory()
    st = db.query(BotState).first() or BotState(is_running=False, trading_mode=mode, exchange="xt")
    st.trading_mode = mode
    st.exchange = "xt"
    db.add(st)
    g = Group(name="silent_spread", dependent_symbol="ETH/USDT:USDT",
              symbols=["ETH/USDT:USDT", "BTC/USDT:USDT"], exchange="xt", status="active")
    db.add(g); db.flush()
    f = OLSFit(group_id=g.id, betas={"BTC/USDT:USDT": 0.04}, intercept=0.0, resid_mean=0.0, resid_std=1.0)
    db.add(f); db.flush()
    now = dt.datetime.utcnow()
    legs = [{"symbol": "ETH/USDT:USDT", "side": "buy", "qty": 0.01, "price": 2500.0},
            {"symbol": "BTC/USDT:USDT", "side": "sell", "qty": 0.0004, "price": 60000.0}]
    db.add(Trade(group_id=g.id, ols_fit_id=f.id, direction="long_residual", mode=mode, entry_time=now,
                 entry_z=-2.1, entry_residual=0, entry_prices={}, status="open" if open_trade else "closed",
                 close_time=None if open_trade else now, pnl=None if open_trade else 3.5,
                 legs_entry=legs, trade_notional=49.0, notional_basis="gross"))
    db.add(Trade(group_id=g.id, ols_fit_id=f.id, direction="short_residual", mode=mode,
                 entry_time=now - dt.timedelta(hours=3), close_time=now - dt.timedelta(hours=1),
                 entry_z=2.3, entry_residual=0, entry_prices={}, status="closed", pnl=-1.25,
                 legs_entry=legs, trade_notional=49.0, notional_basis="gross"))
    db.commit()
    db.close()


def _handler(sf, eng=None, pin=None, clock=None):
    return CommandHandler(session_factory=sf, engine=eng or _engine(), pin=pin,
                          clock=clock or time.monotonic)


def run(coro):
    return asyncio.run(coro)


def text_of(replies):
    return "\n".join(r.text for r in replies)


# ------------------------------------------------------------------ small units
def test_jalali_known_dates():
    assert gregorian_to_jalali(2026, 3, 21) == (1405, 1, 1)
    assert gregorian_to_jalali(2026, 10, 10) == (1405, 7, 18)
    assert fmt_jalali(dt.datetime(2026, 10, 10, 8, 0)) == "1405/07/18 11:30"  # UTC+3:30


def test_parse_chat_ids_and_from_env(monkeypatch):
    assert parse_chat_ids("12, 34;-5  x") == {12, 34, -5}
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert from_env() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "")
    assert from_env() is None  # allow-list is mandatory
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "7")
    assert from_env().chat_ids == {7}


def test_formatters_persian():
    s = fa.format_event("closed", {"group": "g", "trade_id": 3, "mode": "paper", "reason": "stop_loss",
                                   "z": 3.6, "pnl": -4.2, "held_sec": 7200})
    assert "حد ضرر" in s and "-4.20" in s and "2.0 ساعت" in s
    assert "ورود به معامله" in fa.format_event("entered", {"group": "g", "trade_id": 1, "direction": "long_residual",
                                                           "z": -2.2, "gross": 40, "margin": 20, "legs": []})
    assert fa.format_event("unknown_kind", {}) is None


# ------------------------------------------------------------------ commands
def test_status_positions_groups_pnl(sf):
    _seed(sf)
    h = _handler(sf, _engine(diag={1: {"stage": "signal", "name": "silent_spread", "z": 1.2, "at": "x"}}))
    st = text_of(run(h.handle(1, "/status")))
    assert "وضعیت ربات" in st and "پوزیشن باز: 1" in st and "موجودی آزاد پیپر" in st
    pos = text_of(run(h.handle(1, "/positions")))
    assert "silent_spread" in pos and "ETH/USDT:USDT" in pos
    grp = text_of(run(h.handle(1, "/groups")))
    assert "silent_spread" in grp and "منتظر سیگنال" in grp
    pnl = text_of(run(h.handle(1, "/pnl all")))
    assert "-1.25" in pnl
    why = text_of(run(h.handle(1, "/why silent")))
    assert "منتظر سیگنال" in why
    assert "ناشناخته" in text_of(run(h.handle(1, "/nope")))


def test_pause_resume_group_toggle(sf):
    _seed(sf)
    h = _handler(sf)
    run(h.handle(1, "/resume"))
    db = sf(); assert db.query(BotState).first().is_running is True; db.close()
    run(h.handle(1, "/pause"))
    db = sf(); assert db.query(BotState).first().is_running is False; db.close()
    out = text_of(run(h.handle(1, "/group silent_spread off")))
    assert "⛔️" in out  # has an open trade -> same rule as the API
    assert "پیدا نشد" in text_of(run(h.handle(1, "/group nothere on")))


def test_set_whitelist_validation_and_live_confirm(sf):
    _seed(sf)
    h = _handler(sf)
    assert "قابل تغییر نیست" in text_of(run(h.handle(1, "/set xt_leverage 5")))
    assert "نامعتبر" in text_of(run(h.handle(1, "/set z_entry 9")))        # violates stop > entry > close
    assert "→ 2.2" in text_of(run(h.handle(1, "/set z_entry 2.2")))
    db = sf(); assert config_service.get_section(db, "backbone")["z_entry"] == 2.2; db.close()
    # live mode needs confirmation
    db = sf(); db.query(BotState).first().trading_mode = "live"; db.commit(); db.close()
    out = text_of(run(h.handle(1, "/set z_entry 2.4")))
    assert "تأیید" in out
    code = h.pending[1]["code"]
    assert "→ 2.4" in text_of(run(h.handle(1, f"/confirm {code}")))


def test_closeall_confirmation_flow_pin_expiry(sf):
    _seed(sf)
    eng = _engine()
    now = [1000.0]
    h = _handler(sf, eng, pin="9999", clock=lambda: now[0])
    ask = text_of(run(h.handle(1, "/closeall")))
    assert "تأیید" in ask and "PIN" in ask
    code = h.pending[1]["code"]
    assert "نادرست" in text_of(run(h.handle(1, f"/confirm {code} 0000")))   # wrong pin
    assert eng.calls == []
    out = text_of(run(h.handle(1, f"/confirm {code} 9999")))
    assert eng.calls == ["manual_closeall"] and "بستن همه" in out
    # expiry
    run(h.handle(1, "/closeall")); code = h.pending[1]["code"]; now[0] += 61
    assert "مهلت" in text_of(run(h.handle(1, f"/confirm {code} 9999")))
    assert len(eng.calls) == 1
    # three wrong tries cancel it
    run(h.handle(1, "/closeall"))
    for _ in range(3):
        out = text_of(run(h.handle(1, "/confirm 0000 0000")))
    assert "لغو" in out and 1 not in h.pending
    # cancel
    run(h.handle(1, "/closeall")); run(h.handle(1, "/cancel")); assert 1 not in h.pending


def test_closeall_nothing_open(sf):
    _seed(sf, open_trade=False)
    assert "وجود ندارد" in text_of(run(_handler(sf).handle(1, "/closeall")))


def test_digest_text(sf):
    _seed(sf)
    out = run(_handler(sf).build_digest())
    assert "گزارش روزانه" in out and "پوزیشن باز: 1" in out


# ------------------------------------------------------------------ transport (fake Telegram API)
class FakeTelegram:
    def __init__(self, updates=None):
        self.sent, self.updates = [], list(updates or [])

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else {}
        if method == "getUpdates":
            if body.get("offset") == -1:
                return httpx.Response(200, json={"ok": True, "result": []})
            await asyncio.sleep(0.05)
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(200, json={"ok": True, "result": {}})


def _bot(fake, sf, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    handler = _handler(sf)
    return TelegramBot(token="T", chat_ids={42}, handler=handler, http=http, digest_time="off", **kw)


def _msg(uid, text, chat=None, age=0, update_id=1):
    return {"update_id": update_id, "message": {"date": int(time.time()) - age, "text": text,
                                                 "chat": {"id": chat or uid}, "from": {"id": uid}}}


def test_events_become_persian_messages_and_are_throttled(sf):
    async def go():
        fake = FakeTelegram()
        bot = _bot(fake, sf)
        await bot.start()
        emit("closed", group="g", trade_id=1, mode="paper", reason="close", z=0.3, pnl=2.0)
        emit("exit_failed", group="g", trade_id=1, error="boom")
        emit("exit_failed", group="g", trade_id=1, error="boom")   # suppressed duplicate
        await asyncio.sleep(1.5)
        await bot.stop()
        return fake.sent

    sent = run(go())
    texts = [m["text"] for m in sent]
    assert len(texts) == 2 and "بستن معامله" in texts[0] and "بستن معامله ناموفق" in texts[1]
    assert all(m["chat_id"] == 42 and m["parse_mode"] == "HTML" for m in sent)


def test_only_allowlisted_users_get_answers_and_stale_messages_ignored(sf):
    _seed(sf)

    async def go():
        fake = FakeTelegram(updates=[
            _msg(99, "/status", update_id=1),                  # stranger
            _msg(42, "/status", chat=-5, update_id=2),         # right user, wrong chat
            _msg(42, "/status", age=600, update_id=3),         # too old
            _msg(42, "/status", update_id=4),                  # valid
        ])
        bot = _bot(fake, sf)
        await bot.start()
        await asyncio.sleep(1.0)
        await bot.stop()
        return fake.sent

    sent = run(go())
    assert len(sent) == 1 and "وضعیت ربات" in sent[0]["text"] and sent[0]["chat_id"] == 42


def test_next_digest_delay_is_in_the_future():
    bot = TelegramBot(token="T", chat_ids={1}, digest_time="21:00", tz="Asia/Tehran",
                      http=httpx.AsyncClient(transport=httpx.MockTransport(FakeTelegram())))
    d = bot._next_digest_delay()
    assert 0 < d <= 24 * 3600


def test_status_reports_connection(sf):
    bot = _bot(FakeTelegram(), sf)
    s = bot.status()
    assert s["enabled"] is True and s["connected"] is False
