"""Telegram command handling (replies in Persian). Transport lives in telegram.py.

Safety rules:
  * read-only commands run immediately;
  * control commands that can move money (/closeall, /resume and /set while in LIVE mode)
    need a one-time /confirm <code> (+ the PIN if TELEGRAM_PIN is set), valid for 60 seconds;
  * switching Paper/Live, changing the exchange and credentials are NOT possible from here.
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from backend import config_service
from backend.bot_state_service import get_or_create_bot_state
from backend.db import SessionLocal
from backend.models import Group, Trade
from backend.notify import fa
from backend.notify.jalali import fmt_jalali, to_tehran
from backend.strategy.account import leverage_for, paper_free_balance, trade_gross

log = logging.getLogger("telegram.cmd")

CONFIRM_TTL_SEC = 60
MAX_PIN_TRIES = 3

# Settings that may be changed from Telegram (all re-validated by config_service).
SETTABLE = (
    "z_entry", "z_close", "z_stop_loss", "trade_notional", "target_profit_rate",
    "max_open_trades", "max_total_gross_notional", "max_holding_hours", "entry_retry_cooldown_sec",
)


INT_KEYS = ("max_open_trades", "entry_retry_cooldown_sec")


@dataclass
class Reply:
    text: str = ""
    photo: bytes | None = None
    photo_name: str = "chart.png"


class LogRing(logging.Handler):
    """Keeps the last N INFO+ log lines in memory for the /log command."""

    def __init__(self, size: int = 400):
        super().__init__(level=logging.INFO)
        self.buf: collections.deque[str] = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.append(self.format(record).replace("\n", " ")[:300])
        except Exception:
            pass


def _tehran_midnight_utc(days_back: int = 0, tz: str = "Asia/Tehran") -> dt.datetime:
    now = to_tehran(dt.datetime.utcnow(), tz)
    start = (now - dt.timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _trade_pnl(t: Trade):
    from backend.routers.groups_router import _trade_pnl as p
    return p(t)


class CommandHandler:
    def __init__(self, *, session_factory=SessionLocal, engine=None, pin: str | None = None,
                 tz: str = "Asia/Tehran", log_ring: LogRing | None = None, clock=time.monotonic):
        if engine is None:
            from backend.engine.bot_engine import bot_engine as engine
        self.sf = session_factory
        self.engine = engine
        self.pin = (pin or "").strip() or None
        self.tz = tz
        self.log_ring = log_ring
        self.clock = clock
        self.pending: dict[int, dict] = {}
        self.telegram_status: dict[str, Any] = {}  # filled by the transport (for /status)

    # ------------------------------------------------------------------ dispatch
    async def handle(self, chat_id: int, text: str) -> list[Reply]:
        text = (text or "").strip()
        if not text.startswith("/"):
            return [Reply("دستور را با / شروع کنید. برای راهنما: /help")]
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        args = parts[1:]
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            return [Reply("دستور ناشناخته است. برای راهنما: /help")]
        try:
            out = await fn(chat_id, args)
        except HTTPException as e:
            return [Reply(f"⛔️ {fa.esc(e.detail)}")]
        except Exception as e:  # never crash the poller
            log.exception("command %s failed", cmd)
            return [Reply(f"❗ خطا در اجرای دستور: <code>{fa.esc(e)}</code>")]
        if isinstance(out, Reply):
            return [out]
        if isinstance(out, str):
            return [Reply(out)]
        return list(out)

    # ------------------------------------------------------------------ helpers
    def _live(self, db) -> bool:
        return (get_or_create_bot_state(db).trading_mode or "paper") == "live"

    def _open_trades(self, db):
        mode = get_or_create_bot_state(db).trading_mode or "paper"
        return db.query(Trade).filter_by(status="open", mode=mode).all(), mode

    def _find_group(self, db, token: str) -> Group:
        token = token.strip()
        if token.isdigit():
            g = db.get(Group, int(token))
            if g:
                return g
        hits = [g for g in db.query(Group).all() if token.lower() in (g.name or "").lower()]
        exact = [g for g in hits if (g.name or "").lower() == token.lower()]
        if exact:
            return exact[0]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise HTTPException(404, f"گروهی با «{token}» پیدا نشد")
        raise HTTPException(400, "چند گروه مطابقت دارند: " + ", ".join(g.name for g in hits[:8]))

    def _margin(self, db, trade: Trade, backbone: dict) -> tuple[float | None, float | None]:
        g = trade.group
        gross = trade_gross(trade)
        lev = leverage_for(getattr(g, "exchange", None), backbone, g.dependent_symbol)
        return gross, (gross / lev if gross is not None else None)

    def _ask_confirm(self, chat_id: int, action: dict, summary: str) -> Reply:
        code = f"{random.randint(0, 9999):04d}"
        self.pending[chat_id] = {"action": action, "code": code, "expires": self.clock() + CONFIRM_TTL_SEC, "tries": 0}
        pin_hint = " و پین" if self.pin else ""
        sample = f"/confirm {code}" + (" &lt;PIN&gt;" if self.pin else "")
        return Reply(
            f"⚠️ <b>تأیید لازم است</b>\n{summary}\n"
            f"برای اجرا ظرف {CONFIRM_TTL_SEC} ثانیه بفرستید:\n<code>{sample}</code>\n"
            f"(کد{pin_hint} را وارد کنید؛ برای انصراف /cancel)"
        )

    # ------------------------------------------------------------------ info commands
    async def cmd_start(self, chat_id, args):
        return await self.cmd_help(chat_id, args)

    async def cmd_help(self, chat_id, args):
        return Reply(
            "🤖 <b>راهنمای ربات آربیتراژ</b>\n\n"
            "<b>مشاهده</b>\n"
            "/status — وضعیت کلی\n/positions — پوزیشن‌های باز\n/groups — گروه‌ها و دلیل نبودن ورود\n"
            "/why &lt;گروه&gt; — جزئیات یک گروه\n/pnl [today|week|month|all] — سود و زیان\n"
            "/chart — نمودار سرمایه\n/digest — گزارش روزانه\n/log [n] — آخرین لاگ‌ها\n\n"
            "<b>کنترل</b>\n"
            "/pause — توقف ورودهای جدید\n/resume — ازسرگیری ورود\n"
            "/group &lt;گروه&gt; on|off — فعال/غیرفعال کردن گروه\n"
            "/set [پارامتر مقدار] — تنظیمات مجاز\n"
            "/closeall — بستن همه معاملات باز (با تأیید)\n"
            "/confirm &lt;کد&gt; — تأیید عملیات حساس · /cancel\n\n"
            "تغییر Paper/Live، صرافی و کلیدهای API فقط از وب ممکن است."
        )

    async def cmd_status(self, chat_id, args):
        db = self.sf()
        try:
            state = get_or_create_bot_state(db)
            bb = config_service.get_section(db, "backbone")
            trades, mode = self._open_trades(db)
            groups = db.query(Group).all()
            active = [g for g in groups if g.status == "active"]
            margin = 0.0
            for t in trades:
                _, m = self._margin(db, t, bb)
                margin += m or 0.0
            lines = [
                "📊 <b>وضعیت ربات</b>",
                f"حالت: {fa.mode_label(mode)} · صرافی: {fa.esc(state.exchange)}",
                "ورود جدید: " + ("✅ فعال" if state.is_running else "⏹ متوقف"),
            ]
            if getattr(self.engine, "_pause_until", None):
                lines.append(f"⏸ مکث تا: {fa.esc(self.engine._pause_until)}")
            lines.append(f"گروه‌ها: {len(active)} فعال از {len(groups)}")
            lines.append(f"پوزیشن باز: {len(trades)} · مارجین درگیر: {fa.num(margin)}")
            free = paper_free_balance(db, bb) if mode == "paper" else None
            if free is not None:
                lines.append(f"موجودی آزاد پیپر: {fa.num(free)} از {fa.num(bb.get('paper_start_balance'))}")
            orphans = list(getattr(self.engine, "_orphan_positions", []) or [])
            if orphans:
                lines.append(f"🚨 پوزیشن یتیم: {len(orphans)}")
            diag = list((getattr(self.engine, "_group_diag", {}) or {}).values())
            if diag:
                counts: dict[str, int] = {}
                for r in diag:
                    counts[r["stage"]] = counts.get(r["stage"], 0) + 1
                lines.append("مراحل: " + "، ".join(f"{fa.STAGE.get(k, k)}={v}" for k, v in sorted(counts.items())))
            err = getattr(self.engine, "_last_error", None)
            if err:
                lines.append("آخرین خطا: <code>" + fa.esc(str(err).strip().splitlines()[-1][:200]) + "</code>")
            lines.append(f"زمان: {fa.fmt_now()}")
            return "\n".join(lines)
        finally:
            db.close()

    async def cmd_positions(self, chat_id, args):
        db = self.sf()
        try:
            bb = config_service.get_section(db, "backbone")
            trades, mode = self._open_trades(db)
            if not trades:
                return f"پوزیشن بازی وجود ندارد ({fa.mode_label(mode)})."
            out = [f"📂 <b>پوزیشن‌های باز</b> · {fa.mode_label(mode)}"]
            for t in trades:
                gross, margin = self._margin(db, t, bb)
                age_h = (dt.datetime.utcnow() - t.entry_time).total_seconds() / 3600 if t.entry_time else 0
                out.append(
                    f"\n<b>{fa.esc(t.group.name)}</b> (#{t.id}) · {fa.DIRECTION.get(t.direction, t.direction)}\n"
                    f"باز شده: {fmt_jalali(t.entry_time, self.tz)} ({fa.num(age_h, 1)} ساعت) · z={fa.num(t.entry_z)}\n"
                    f"حجم کل: {fa.num(gross)} · مارجین: {fa.num(margin)}\n"
                    f"{fa._legs(t.legs_entry)}"
                )
            return "\n".join(out)[:4000]
        finally:
            db.close()

    async def cmd_groups(self, chat_id, args):
        db = self.sf()
        try:
            groups = db.query(Group).filter(Group.status.in_(["active", "inactive"])).order_by(Group.id).all()
            diag = getattr(self.engine, "_group_diag", {}) or {}
            if not groups:
                return "گروهی وجود ندارد."
            out = ["🗂 <b>گروه‌ها</b>"]
            for g in groups[:60]:
                d = diag.get(g.id)
                stage = fa.STAGE.get(d["stage"], d["stage"]) if d else "—"
                z = f" · z={fa.num(d['z'])}" if d and d.get("z") is not None else ""
                st = "🟢" if g.status == "active" else "⚪️"
                out.append(f"{st} {fa.esc(g.name)} — {stage}{z}")
            return "\n".join(out)[:4000]
        finally:
            db.close()

    async def cmd_why(self, chat_id, args):
        if not args:
            return "نام یا شماره گروه را بدهید: /why نام‌گروه"
        db = self.sf()
        try:
            g = self._find_group(db, " ".join(args))
            d = (getattr(self.engine, "_group_diag", {}) or {}).get(g.id)
            if not d:
                return f"برای «{fa.esc(g.name)}» هنوز ارزیابی‌ای ثبت نشده (ربات متوقف است یا گروه غیرفعال)."
            lines = [f"🔎 <b>{fa.esc(g.name)}</b>", f"مرحله: {fa.STAGE.get(d['stage'], d['stage'])}"]
            if d.get("text"):
                lines.append(fa.esc(d["text"])[:500])
            if d.get("z") is not None:
                lines.append(f"z اکنون: {fa.num(d['z'])} (آستانه {fa.num(d.get('z_entry'))})")
            if d.get("stationary") is not None:
                p = f" (p={fa.num(d.get('adf_p'), 3)})" if d.get("adf_p") is not None else ""
                lines.append("پایایی: " + ("بله" if d["stationary"] else "خیر") + p)
            lines.append(f"آخرین ارزیابی: {fa.esc(d.get('at'))}")
            return "\n".join(lines)
        finally:
            db.close()

    async def cmd_pnl(self, chat_id, args):
        period = (args[0].lower() if args else "today")
        starts = {"today": _tehran_midnight_utc(0, self.tz), "week": _tehran_midnight_utc(6, self.tz),
                  "month": _tehran_midnight_utc(29, self.tz), "all": None}
        if period not in starts:
            return "بازه نامعتبر: today | week | month | all"
        db = self.sf()
        try:
            mode = get_or_create_bot_state(db).trading_mode or "paper"
            q = db.query(Trade).filter_by(status="closed", mode=mode)
            if starts[period]:
                q = q.filter(Trade.close_time >= starts[period])
            rows = [(t, _trade_pnl(t)) for t in q.all()]
            rows = [(t, p) for t, p in rows if p is not None]
            if not rows:
                return f"معامله بسته‌شده‌ای در بازه «{period}» نیست ({fa.mode_label(mode)})."
            total = sum(p for _, p in rows)
            wins = sum(1 for _, p in rows if p > 0)
            by_group: dict[str, float] = {}
            for t, p in rows:
                by_group[t.group.name] = by_group.get(t.group.name, 0.0) + p
            top = sorted(by_group.items(), key=lambda kv: kv[1], reverse=True)
            lines = [
                f"💰 <b>سود و زیان ({period})</b> · {fa.mode_label(mode)}",
                f"معاملات: {len(rows)} · برنده: {wins} ({fa.num(100 * wins / len(rows), 0)}٪)",
                f"جمع: <b>{fa.signed(total)}</b>",
            ]
            if top:
                lines.append("بهترین: " + "، ".join(f"{fa.esc(n)} {fa.signed(v)}" for n, v in top[:3]))
                if len(top) > 3:
                    lines.append("بدترین: " + "، ".join(f"{fa.esc(n)} {fa.signed(v)}" for n, v in top[-3:][::-1]))
            return "\n".join(lines)
        finally:
            db.close()

    async def cmd_log(self, chat_id, args):
        n = 25
        if args and args[0].isdigit():
            n = max(1, min(60, int(args[0])))
        if not self.log_ring or not self.log_ring.buf:
            return "لاگی ثبت نشده است."
        lines = list(self.log_ring.buf)[-n:]
        return "<pre>" + fa.esc("\n".join(lines))[-3800:] + "</pre>"

    async def cmd_digest(self, chat_id, args):
        return Reply(await self.build_digest())

    async def cmd_chart(self, chat_id, args):
        db = self.sf()
        try:
            mode = get_or_create_bot_state(db).trading_mode or "paper"
            bb = config_service.get_section(db, "backbone")
            rows = db.query(Trade).filter_by(status="closed", mode=mode).order_by(Trade.close_time.asc()).all()
            pts = [(t.close_time, _trade_pnl(t)) for t in rows if t.close_time and _trade_pnl(t) is not None]
            if not pts:
                return "هنوز معامله بسته‌شده‌ای برای نمودار نیست."
            start = float(bb.get("paper_start_balance", 0) or 0) if mode == "paper" else 0.0
            try:
                import io
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
            except Exception:
                return "برای نمودار باید matplotlib نصب باشد (pip install matplotlib)."
            xs, ys, cum = [], [], start
            for ts, p in pts:
                cum += p
                xs.append(to_tehran(ts, self.tz).replace(tzinfo=None))
                ys.append(cum)
            fig, ax = plt.subplots(figsize=(7, 3.2), dpi=130)
            ax.plot(xs, ys, color="#6b9a6b" if ys[-1] >= (start or 0) else "#b85c4a", lw=1.8)
            ax.axhline(start, color="#888", lw=0.8, ls="--")
            ax.set_title(f"Equity ({mode})", fontsize=10)
            ax.grid(alpha=0.25)
            fig.autofmt_xdate()
            buf = io.BytesIO()
            fig.tight_layout()
            fig.savefig(buf, format="png")
            plt.close(fig)
            return Reply(f"نمودار سرمایه · {len(pts)} معامله", photo=buf.getvalue(), photo_name="equity.png")
        finally:
            db.close()

    # ------------------------------------------------------------------ control commands
    async def cmd_pause(self, chat_id, args):
        from backend.routers import bot_router
        db = self.sf()
        try:
            bot_router.stop_bot(db)
        finally:
            db.close()
        return "⏹ ورودهای جدید متوقف شد؛ پوزیشن‌های باز همچنان مدیریت می‌شوند."

    async def cmd_resume(self, chat_id, args):
        db = self.sf()
        try:
            live = self._live(db)
        finally:
            db.close()
        if live:
            return self._ask_confirm(chat_id, {"type": "resume"}, "ازسرگیری ورودهای جدید در حالت <b>واقعی (Live)</b>")
        return await self._do_resume()

    async def _do_resume(self):
        from backend.routers import bot_router
        db = self.sf()
        try:
            bot_router.start_bot(db)
        finally:
            db.close()
        return "▶️ ورودهای جدید فعال شد."

    async def cmd_group(self, chat_id, args):
        if len(args) < 2 or args[-1].lower() not in ("on", "off"):
            return "استفاده: /group نام‌گروه on|off"
        from backend.routers import groups_router
        from backend.schemas import GroupStatusUpdate
        db = self.sf()
        try:
            g = self._find_group(db, " ".join(args[:-1]))
            status = "active" if args[-1].lower() == "on" else "inactive"
            groups_router.set_group_status(g.id, GroupStatusUpdate(status=status), db)
            return f"✅ گروه «{fa.esc(g.name)}» {'فعال' if status == 'active' else 'غیرفعال'} شد."
        finally:
            db.close()

    async def cmd_set(self, chat_id, args):
        db = self.sf()
        try:
            bb = config_service.get_section(db, "backbone")
            if len(args) < 2:
                lines = ["⚙️ <b>تنظیمات قابل تغییر</b>"] + [f"<code>{k}</code> = {bb.get(k)}" for k in SETTABLE]
                lines.append("\nمثال: /set z_entry 2.2")
                return "\n".join(lines)
            key, raw = args[0], args[1]
            if key not in SETTABLE:
                return "این پارامتر از تلگرام قابل تغییر نیست. مجاز: " + ", ".join(SETTABLE)
            try:
                value = float(raw)
            except ValueError:
                return "مقدار باید عدد باشد."
            if key in INT_KEYS:
                if value != int(value):
                    return "این پارامتر باید عدد صحیح باشد."
                value = int(value)
            live = self._live(db)
        finally:
            db.close()
        if live:
            return self._ask_confirm(chat_id, {"type": "set", "key": key, "value": value},
                                     f"تغییر <code>{key}</code> به <b>{value}</b> در حالت <b>Live</b>")
        return self._do_set(key, value)

    def _do_set(self, key: str, value) -> str:
        db = self.sf()
        try:
            old = config_service.get_section(db, "backbone").get(key)
            try:
                config_service.update_section(db, "backbone", {key: value})
            except ValueError as e:
                return f"⛔️ مقدار نامعتبر: {fa.esc(e)}"
        finally:
            db.close()
        from backend.notify import emit
        emit("bot_state", what="config", key=key, value=f"{old} → {value}", via="تلگرام")
        return f"✅ {key}: {old} → {value}"

    async def cmd_closeall(self, chat_id, args):
        db = self.sf()
        try:
            trades, mode = self._open_trades(db)
            names = ", ".join(sorted({t.group.name for t in trades}))
        finally:
            db.close()
        if not trades:
            return "معامله بازی برای بستن وجود ندارد."
        return self._ask_confirm(
            chat_id, {"type": "closeall"},
            f"بستن <b>{len(trades)}</b> معامله باز ({fa.mode_label(mode)}) و توقف ورودهای جدید.\nگروه‌ها: {fa.esc(names)}",
        )

    async def cmd_cancel(self, chat_id, args):
        self.pending.pop(chat_id, None)
        return "انصراف داده شد."

    async def cmd_confirm(self, chat_id, args):
        p = self.pending.get(chat_id)
        if not p or self.clock() > p["expires"]:
            self.pending.pop(chat_id, None)
            return "عملیات در انتظار تأییدی وجود ندارد (یا مهلت تمام شده)."
        code = args[0] if args else ""
        pin = args[1] if len(args) > 1 else ""
        if code != p["code"] or (self.pin and pin != self.pin):
            p["tries"] += 1
            if p["tries"] >= MAX_PIN_TRIES:
                self.pending.pop(chat_id, None)
                return "🚫 تلاش‌های ناموفق زیاد؛ عملیات لغو شد."
            return "کد یا پین نادرست است."
        self.pending.pop(chat_id, None)
        act = p["action"]
        if act["type"] == "closeall":
            return await self._do_closeall()
        if act["type"] == "resume":
            return await self._do_resume()
        if act["type"] == "set":
            return self._do_set(act["key"], act["value"])
        return "عملیات ناشناخته."

    async def _do_closeall(self) -> str:
        res = await self.engine.close_all(reason="manual_closeall")
        lines = [f"🧹 بستن همه: {res.get('closed', 0)} از {res.get('total', 0)} معامله بسته شد."]
        if res.get("failed"):
            lines.append(f"⚠️ {res['failed']} معامله بسته نشد؛ ربات تلاش را ادامه می‌دهد.")
        lines.append("ورودهای جدید متوقف است (برای ازسرگیری /resume).")
        return "\n".join(lines)

    # ------------------------------------------------------------------ digest
    async def build_digest(self) -> str:
        db = self.sf()
        try:
            state = get_or_create_bot_state(db)
            bb = config_service.get_section(db, "backbone")
            mode = state.trading_mode or "paper"
            today = _tehran_midnight_utc(0, self.tz)
            week = _tehran_midnight_utc(6, self.tz)
            closed = db.query(Trade).filter_by(status="closed", mode=mode).all()

            def agg(since):
                xs = [_trade_pnl(t) for t in closed if t.close_time and (since is None or t.close_time >= since)]
                xs = [x for x in xs if x is not None]
                return len(xs), sum(1 for x in xs if x > 0), sum(xs)

            n1, w1, p1 = agg(today)
            n7, w7, p7 = agg(week)
            trades, _ = self._open_trades(db)
            margin = sum((self._margin(db, t, bb)[1] or 0.0) for t in trades)
            lines = [
                f"🗓 <b>گزارش روزانه</b> · {fa.mode_label(mode)} · {fmt_jalali(dt.datetime.utcnow(), self.tz, with_time=False)}",
                f"امروز: {n1} معامله ({w1} برنده) · <b>{fa.signed(p1)}</b>",
                f"۷ روز اخیر: {n7} معامله ({w7} برنده) · <b>{fa.signed(p7)}</b>",
                f"پوزیشن باز: {len(trades)} · مارجین درگیر: {fa.num(margin)}",
            ]
            free = paper_free_balance(db, bb) if mode == "paper" else None
            if free is not None:
                lines.append(f"موجودی آزاد پیپر: {fa.num(free)} از {fa.num(bb.get('paper_start_balance'))}")
            diag = list((getattr(self.engine, "_group_diag", {}) or {}).values())
            if diag:
                counts: dict[str, int] = {}
                for r in diag:
                    counts[r["stage"]] = counts.get(r["stage"], 0) + 1
                lines.append("وضعیت گروه‌ها: " + "، ".join(f"{fa.STAGE.get(k, k)}={v}" for k, v in sorted(counts.items())))
            orphans = list(getattr(self.engine, "_orphan_positions", []) or [])
            if orphans:
                lines.append(f"🚨 پوزیشن یتیم: {len(orphans)}")
            return "\n".join(lines)
        finally:
            db.close()
