"""Persian message formatting for the Telegram bot. Pure functions: easy to test."""
from __future__ import annotations

import datetime as dt
import html

from backend.notify.jalali import fmt_jalali

DIRECTION = {
    "long_residual": "لانگ رزیدوال (خرید وابسته)",
    "short_residual": "شورت رزیدوال (فروش وابسته)",
}
SIDE = {"buy": "خرید", "sell": "فروش"}
REASON = {
    "close": "بازگشت به میانگین",
    "stop_loss": "حد ضرر",
    "time_stop": "توقف زمانی",
    "risk_exit": "خروج ریسک (نزدیک لیکوئید)",
    "manual_closeall": "بستن دستی همه",
    "entry_rollback": "بازگشت ورودی ناقص",
}
STAGE = {
    "entered": "وارد شد",
    "position_open": "پوزیشن باز",
    "bot_stopped": "ربات متوقف",
    "paused": "مکث (محدودیت صرافی)",
    "cooldown": "استراحت بعد از خطا",
    "signal": "منتظر سیگنال",
    "no_signal": "منتظر سیگنال",
    "not_stationary": "ناپایا (رد شد)",
    "profit_gate": "سود مورد انتظار کم",
    "half_life": "نیمه‌عمر نامناسب",
    "data_stale": "داده قدیمی",
    "no_data": "داده کم",
    "fit_failed": "برازش ناموفق",
    "portfolio_cap": "سقف پرتفوی",
    "orphan_block": "پوزیشن یتیم",
    "symbol_clash": "تداخل نماد",
    "scale_cap": "سبد خیلی کوچک (سقف مقیاس)",
    "sizing_blocked": "موجودی/حجم ناکافی",
    "order_failed": "سفارش ناموفق",
    "config_error": "خطای پیکربندی",
    "gate_error": "خطای فیلتر سود",
}


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def num(x, d: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.{d}f}"


def signed(x, d: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:+,.{d}f}"


def mode_label(mode: str | None) -> str:
    return "واقعی (Live)" if mode == "live" else "آزمایشی (Paper)"


def _legs(legs: list[dict] | None) -> str:
    lines = []
    for l in legs or []:
        q = l.get("filled_qty") if l.get("filled_qty") is not None else l.get("qty")
        px = l.get("fill_price") or l.get("price")
        lines.append(
            f"  • {esc(l.get('symbol'))}: {SIDE.get(l.get('side'), l.get('side'))} "
            f"{num(q, 6)} @ {num(px, 4)}"
        )
    return "\n".join(lines) or "  —"


def fmt_entered(d: dict) -> str:
    return (
        f"🟢 <b>ورود به معامله</b> · {mode_label(d.get('mode'))}\n"
        f"گروه: <b>{esc(d.get('group'))}</b> (#{d.get('trade_id')})\n"
        f"جهت: {DIRECTION.get(d.get('direction'), esc(d.get('direction')))}\n"
        f"z ورود: {num(d.get('z'))}\n"
        f"حجم کل: {num(d.get('gross'))} · مارجین: {num(d.get('margin'))}\n"
        f"پایه‌ها:\n{_legs(d.get('legs'))}"
    )


def fmt_closed(d: dict) -> str:
    pnl = d.get("pnl")
    icon = "✅" if (pnl is not None and float(pnl) >= 0) else "🔻"
    held = ""
    if d.get("held_sec") is not None:
        h = float(d["held_sec"]) / 3600.0
        held = f"\nمدت نگهداری: {num(h, 1)} ساعت"
    src = "" if d.get("pnl_source") in (None, "model") else " (صرافی)"
    return (
        f"{icon} <b>بستن معامله</b> · {mode_label(d.get('mode'))}\n"
        f"گروه: <b>{esc(d.get('group'))}</b> (#{d.get('trade_id')})\n"
        f"دلیل: {REASON.get(d.get('reason'), esc(d.get('reason')))}\n"
        f"z خروج: {num(d.get('z'))}\n"
        f"سود/زیان: <b>{signed(pnl)}</b>{src}{held}"
    )


def fmt_entry_failed(d: dict) -> str:
    return (
        "⚠️ <b>ورود ناموفق</b>\n"
        f"گروه: <b>{esc(d.get('group'))}</b>\n"
        f"{esc(d.get('error'))}\n"
        + ("پایه‌های پرشده بازگردانده شدند." if d.get("rolled_back") else "")
    )


def fmt_rollback_failed(d: dict) -> str:
    return (
        "🚨 <b>بازگشت ورودی ناموفق — پوزیشن باز بدون پوشش ممکن است وجود داشته باشد</b>\n"
        f"گروه: <b>{esc(d.get('group'))}</b>\n"
        f"نمادها: {esc(', '.join(d.get('symbols') or []))}\n"
        "لطفاً فوراً صرافی را بررسی کنید."
    )


def fmt_exit_failed(d: dict) -> str:
    return (
        "🚨 <b>بستن معامله ناموفق</b>\n"
        f"گروه: <b>{esc(d.get('group'))}</b> (#{d.get('trade_id')})\n"
        f"{esc(d.get('error'))}\n"
        "ربات در چرخه بعد دوباره تلاش می‌کند."
    )


def fmt_orphan(d: dict) -> str:
    rows = "\n".join(
        f"  • {esc(o.get('symbol'))} {esc(o.get('side'))} حجم {num(o.get('liability'), 6)}"
        for o in d.get("orphans") or []
    )
    return (
        "🚨 <b>پوزیشن یتیم در صرافی</b> (ربات از آن خبر ندارد)\n"
        f"{rows}\nورود روی این نمادها مسدود شد."
    )


def fmt_risk_exit(d: dict) -> str:
    return (
        "🚨 <b>نزدیک قیمت لیکوئید — خروج اضطراری</b>\n"
        f"گروه: <b>{esc(d.get('group'))}</b> (#{d.get('trade_id')})"
    )


def fmt_rate_limit(d: dict) -> str:
    return f"⏸ <b>محدودیت نرخ صرافی</b>\nورودها تا {esc(d.get('until'))} متوقف شد."


def fmt_cycle_error(d: dict) -> str:
    return (
        "❗ <b>خطا در چرخه ربات</b>\n"
        + (f"گروه: {esc(d.get('group'))}\n" if d.get("group") else "")
        + f"<code>{esc(str(d.get('error'))[:600])}</code>"
    )


def fmt_bot_state(d: dict) -> str:
    what = d.get("what")
    text = {
        "started": "▶️ ربات فعال شد (ورود جدید مجاز)",
        "stopped": "⏹ ربات متوقف شد (فقط مدیریت پوزیشن‌های باز)",
        "mode": f"🔁 حالت به {mode_label(d.get('value'))} تغییر کرد",
        "exchange": f"🔁 صرافی فعال: {esc(d.get('value'))}",
        "group": f"🗂 گروه {esc(d.get('group'))}: {esc(d.get('value'))}",
        "config": f"⚙️ تنظیم {esc(d.get('key'))} = {esc(d.get('value'))}",
        "cleared": f"🧹 تاریخچه گروه {esc(d.get('group'))} پاک شد ({d.get('n')} معامله)",
    }.get(what, esc(what))
    src = f"\nاز طریق: {esc(d.get('via'))}" if d.get("via") else ""
    return text + src


def fmt_engine(d: dict) -> str:
    if d.get("what") == "shutdown":
        return "🛑 سرور ربات در حال خاموش شدن است."
    return f"🤖 ربات بالا آمد · {mode_label(d.get('mode'))} · صرافی {esc(d.get('exchange'))}"


FORMATTERS = {
    "entered": fmt_entered,
    "closed": fmt_closed,
    "entry_failed": fmt_entry_failed,
    "rollback_failed": fmt_rollback_failed,
    "exit_failed": fmt_exit_failed,
    "orphan": fmt_orphan,
    "risk_exit": fmt_risk_exit,
    "rate_limit": fmt_rate_limit,
    "cycle_error": fmt_cycle_error,
    "bot_state": fmt_bot_state,
    "engine": fmt_engine,
}

# kinds that must reach the user even when something else is flooding; repeated ones are
# suppressed for this many seconds per key
SUPPRESS_SEC = {
    "rollback_failed": 1800, "exit_failed": 1800, "orphan": 3600, "risk_exit": 1800,
    "rate_limit": 600, "cycle_error": 1800, "entry_failed": 300,
}


def format_event(kind: str, data: dict) -> str | None:
    f = FORMATTERS.get(kind)
    return f(data) if f else None


def fmt_now(ts: dt.datetime | None = None) -> str:
    return fmt_jalali(ts or dt.datetime.utcnow())
