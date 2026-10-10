"""Gregorian -> Jalali (Solar Hijri) conversion, enough for message timestamps."""
from __future__ import annotations

import datetime as dt

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def to_tehran(ts: dt.datetime | None, tz: str = "Asia/Tehran") -> dt.datetime | None:
    """Naive datetimes in this app are UTC."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if ZoneInfo is not None:
        try:
            return ts.astimezone(ZoneInfo(tz))
        except Exception:
            pass
    return ts.astimezone(dt.timezone(dt.timedelta(hours=3, minutes=30)))


def fmt_jalali(ts: dt.datetime | None, tz: str = "Asia/Tehran", with_time: bool = True) -> str:
    local = to_tehran(ts, tz)
    if local is None:
        return "—"
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    s = f"{jy:04d}/{jm:02d}/{jd:02d}"
    return f"{s} {local:%H:%M}" if with_time else s
