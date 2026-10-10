#!/usr/bin/env python3
"""External watchdog: alerts on Telegram when the bot stops writing its heartbeat.

The bot cannot report its own death, so run this from cron or a systemd timer (every 1-5
minutes), independently of the bot process. Standard library only.

    python scripts/watchdog.py [--max-age 600]

It reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS (and optional TELEGRAM_PROXY) from the
environment or the project's .env, looks at data/heartbeat (a unix timestamp that the
trading loop refreshes every cycle) and sends ONE alert when it goes stale and ONE
message when it recovers. State is kept in data/watchdog.state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send(token: str, chat_id: str, text: str, proxy: str | None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": proxy, "http": proxy}) if proxy else urllib.request.ProxyHandler()
    )
    opener.open(urllib.request.Request(url, data=body), timeout=20).read()


def heartbeat_age(path: Path, now: float) -> float | None:
    try:
        return now - float(path.read_text().strip())
    except Exception:
        return None


def decide(age: float | None, max_age: float, was_down: bool) -> str | None:
    """Return 'down', 'up' or None (no message) given the current state."""
    stale = age is None or age > max_age
    if stale and not was_down:
        return "down"
    if not stale and was_down:
        return "up"
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=float, default=600.0, help="seconds without heartbeat before alerting")
    ap.add_argument("--heartbeat", default=str(DATA / "heartbeat"))
    ap.add_argument("--state", default=str(DATA / "watchdog.state"))
    args = ap.parse_args(argv)

    load_env(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    ids = [x for x in os.environ.get("TELEGRAM_CHAT_IDS", "").replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    proxy = os.environ.get("TELEGRAM_PROXY", "").strip() or None
    if not token or not ids:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS not set", file=sys.stderr)
        return 2

    state_path = Path(args.state)
    try:
        was_down = bool(json.loads(state_path.read_text()).get("down"))
    except Exception:
        was_down = False

    age = heartbeat_age(Path(args.heartbeat), time.time())
    verdict = decide(age, args.max_age, was_down)
    if verdict is None:
        return 0
    if verdict == "down":
        msg = ("🚨 ربات آربیتراژ پاسخ نمی‌دهد: "
               + ("فایل heartbeat وجود ندارد." if age is None else f"آخرین چرخه {int(age // 60)} دقیقه پیش بوده.")
               + "\nسرور/سرویس را بررسی کنید (systemctl status).")
    else:
        msg = "✅ ربات دوباره فعال شد."
    try:
        for cid in ids:
            send(token, cid.strip(), msg, proxy)
    except Exception as e:  # keep the old state so the next run retries
        print(f"send failed: {e}", file=sys.stderr)
        return 1
    state_path.write_text(json.dumps({"down": verdict == "down", "at": time.time()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
