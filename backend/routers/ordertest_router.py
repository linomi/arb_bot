"""TEMPORARY: manual XT order test (open one position, close it, record everything).

Purpose: measure what the exchange really does on a live order -- fill price, slippage against the
quote, fees, filled size, leverage actually applied, position data, balance change -- through the
SAME code path the bot uses (BotEngine._place_legs on an XTClient), so the numbers can be inspected
and the live fill logic validated with a few small orders.

REAL ORDERS ON YOUR LIVE XT ACCOUNT. Hard caps below. Remove this feature after use:
  delete backend/routers/ordertest_router.py, frontend/js/ordertest.js, tests/test_ordertest.py,
  and the lines marked "ordertest (temporary)" in backend/main.py, backend/engine/bot_engine.py,
  backend/logstore.py, frontend/index.html, frontend/js/main.js, frontend/css/style_extra.css.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import config_service, security
from backend.db import DATA_DIR, get_db
from backend.models import Credential, Trade
from backend.strategy.pnl import fills_pnl_and_fee
from backend.strategy.sizing import close_legs_from_entry

log = logging.getLogger("ordertest")
router = APIRouter(prefix="/api/ordertest", tags=["ordertest"])

MAX_NOTIONAL = 100.0   # USDT, hard cap per test order
MAX_LEVERAGE = 20
_LOG_PATH = DATA_DIR / "ordertest_log.jsonl"
_STATE_PATH = DATA_DIR / "ordertest_state.json"


# ----------------------------------------------------------------------------- small utils
def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def _ms(t0: float) -> int:
    return int(round((time.perf_counter() - t0) * 1000))


def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"open": {}}


def _save_state(st: dict) -> None:
    _STATE_PATH.write_text(json.dumps(st, default=str), encoding="utf-8")


def active_symbols() -> set[str]:
    """Symbols with a test position currently open (the bot's orphan check must ignore them)."""
    try:
        return set(_load_state().get("open", {}))
    except Exception:
        return set()


def _write_event(event: dict) -> None:
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("could not write ordertest log: %s", e)


def _pct(a: float | None, b: float | None) -> float | None:
    """(a / b - 1) in percent."""
    try:
        return (float(a) / float(b) - 1.0) * 100.0 if a and b else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _build_client(db: Session, leverage: int):
    from backend.exchange.xt import XTClient
    cred = db.query(Credential).filter_by(exchange="xt").first()
    if cred is None or cred.auth_method != "key_secret" or not (cred.encrypted_api_key and cred.encrypted_api_secret):
        raise HTTPException(400, "XT API key + secret are not saved (Settings → Credentials, exchange XT).")
    backbone = config_service.get_section(db, "backbone")
    return XTClient(
        api_key=security.decrypt(cred.encrypted_api_key),
        api_secret=security.decrypt(cred.encrypted_api_secret),
        leverage=int(leverage),
        margin_mode=str(backbone.get("xt_margin_mode", "isolated")),
    )


# ----------------------------------------------------------------------------- snapshots
async def _market_snapshot(client, symbol: str) -> dict:
    out: dict[str, Any] = {"at": _now()}
    t0 = time.perf_counter()
    try:
        tk = await client._ex.fetch_ticker(symbol)
        out["last"], out["bid"], out["ask"] = tk.get("last"), tk.get("bid"), tk.get("ask")
        out["mark"] = (tk.get("info") or {}).get("markPrice") if isinstance(tk.get("info"), dict) else None
    except Exception as e:
        out["ticker_error"] = str(e)[:200]
    try:
        ob = await client._ex.fetch_order_book(symbol, 5)
        out["book_bids"] = (ob.get("bids") or [])[:3]
        out["book_asks"] = (ob.get("asks") or [])[:3]
        if ob.get("bids") and ob.get("asks"):
            out["bid"], out["ask"] = ob["bids"][0][0], ob["asks"][0][0]
    except Exception as e:
        out["book_error"] = str(e)[:200]
    if out.get("bid") and out.get("ask"):
        out["mid"] = (float(out["bid"]) + float(out["ask"])) / 2.0
        out["spread_pct"] = _pct(out["ask"], out["bid"])
    out["took_ms"] = _ms(t0)
    return out


async def _balance_snapshot(client) -> dict:
    out: dict[str, Any] = {"at": _now()}
    try:
        bal = await client._ex.fetch_balance({"type": "swap"})
        u = bal.get("USDT") or {}
        out.update(total=u.get("total"), free=u.get("free"), used=u.get("used"))
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


async def _positions_snapshot(client, symbol: str) -> list[dict]:
    try:
        return await client.list_positions(symbol=symbol)
    except Exception as e:
        return [{"error": str(e)[:200]}]


def _slim_position(p: dict | None) -> dict | None:
    if not p or p.get("error"):
        return p
    raw = p.get("_raw") or {}
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    return {
        "side": p.get("side"), "contracts": p.get("contracts"), "contract_size": p.get("contractSize"),
        "base_qty": p.get("liability"), "entry_price": p.get("entryPrice"), "mark_price": p.get("markPrice"),
        "liquidation_price": p.get("liquidationPrice"), "margin_ratio": p.get("marginRatio"),
        "unrealized_pnl": p.get("unrealizedPNL"),
        "leverage": raw.get("leverage"), "margin_mode": raw.get("marginMode"),
        "initial_margin": raw.get("initialMargin"), "notional": raw.get("notional"),
        "raw_info": info,
    }


# ----------------------------------------------------------------------------- models
class OpenIn(BaseModel):
    symbol: str
    side: Literal["long", "short"]
    notional: float
    leverage: int
    confirm: bool = False


class CloseIn(BaseModel):
    symbol: str
    confirm: bool = False


# ----------------------------------------------------------------------------- endpoints
@router.get("/symbols")
async def symbols(db: Session = Depends(get_db)):
    from backend.exchange.xt import XTClient
    client = XTClient(api_key=None, api_secret=None)
    try:
        syms = await client.get_liquid_symbols(top_n=150, quote="USDT")
        rows = []
        for s in syms:
            m = client._ex.markets.get(s, {})
            lev = ((m.get("limits") or {}).get("leverage") or {})
            rows.append({"symbol": s, "contract_size": m.get("contractSize"),
                         "max_leverage": lev.get("max"), "min_cost": ((m.get("limits") or {}).get("cost") or {}).get("min")})
        return {"symbols": rows, "max_notional": MAX_NOTIONAL, "max_leverage": MAX_LEVERAGE}
    except Exception as e:
        raise HTTPException(502, f"could not load XT symbols: {e}")
    finally:
        await client.aclose()


@router.get("/state")
def state():
    st = _load_state()
    return {"open": st.get("open", {}), "max_notional": MAX_NOTIONAL, "max_leverage": MAX_LEVERAGE}


@router.get("/log")
def download_log():
    try:
        text = _LOG_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="ordertest_log.jsonl"'})


@router.post("/open")
async def open_position(body: OpenIn, db: Session = Depends(get_db)):
    from backend.engine.bot_engine import BotEngine
    if not body.confirm:
        raise HTTPException(400, "Tick the confirmation box: this places a REAL order on your XT account.")
    if not (1.0 <= body.notional <= MAX_NOTIONAL):
        raise HTTPException(400, f"notional must be between 1 and {MAX_NOTIONAL:g} USDT")
    if not (1 <= body.leverage <= MAX_LEVERAGE):
        raise HTTPException(400, f"leverage must be between 1 and {MAX_LEVERAGE}")

    client = _build_client(db, body.leverage)
    ev: dict[str, Any] = {"kind": "open", "started_at": _now(), "request": body.model_dump()}
    try:
        await client._ensure_markets()
        symbol = client._normalize_symbol(body.symbol)
        client._assert_linear_usdt_swap(symbol)
        ev["symbol"] = symbol

        st = _load_state()
        if symbol in st.get("open", {}):
            raise HTTPException(409, f"a test position on {symbol} is already open - close it first")
        in_use = db.query(Trade).filter(Trade.status.in_(("open", "failed_partial")), Trade.mode == "live").all()
        if any(l.get("symbol") == symbol for t in in_use for l in (t.legs_entry or [])):
            raise HTTPException(409, f"{symbol} is used by an open live bot trade - pick another coin")
        before_pos = await _positions_snapshot(client, symbol)
        if any(float(p.get("liability") or 0) > 0 for p in before_pos if not p.get("error")):
            raise HTTPException(409, f"there is already a position on {symbol} in the account - pick another coin")

        market = await _market_snapshot(client, symbol)
        bal_before = await _balance_snapshot(client)
        last = market.get("last") or market.get("mid")
        if not last:
            raise HTTPException(502, f"no price for {symbol}: {market}")
        side_order = "buy" if body.side == "long" else "sell"
        leg = {"symbol": symbol, "side": side_order, "qty": body.notional / float(last), "price": float(last),
               "is_dependent": True}
        eng = BotEngine()
        legs = await eng._preflight_legs(client, [leg], "xt")

        t0 = time.perf_counter()
        placed = await eng._place_legs(client, legs, is_close=False, attempt_id="ordertest")
        order_ms = _ms(t0)
        leg = placed[0]

        fill = None
        try:
            fill = await client.get_order_fill(leg.get("order_id"), symbol, attempts=2, delay=0.3)
        except Exception as e:
            ev["fill_lookup_error"] = str(e)[:200]
        pos_after = await _positions_snapshot(client, symbol)
        market_after = await _market_snapshot(client, symbol)
        bal_after = await _balance_snapshot(client)

        fp = leg.get("fill_price")
        touch = market.get("ask") if side_order == "buy" else market.get("bid")
        sign = 1.0 if side_order == "buy" else -1.0  # positive = paid more / received less than reference
        ev.update(
            ended_at=_now(), order_total_ms=order_ms,
            before={"market": market, "balance": bal_before, "positions": [_slim_position(p) for p in before_pos]},
            after={"market": market_after, "balance": bal_after, "positions": [_slim_position(p) for p in pos_after]},
            leg={k: v for k, v in leg.items() if k not in ("order_response",)},
            order_response=leg.get("order_response"),
            exchange_fill={k: v for k, v in (fill or {}).items() if k != "raw"} or None,
            exchange_fill_raw=(fill or {}).get("raw"),
            analysis={
                "decision_price(last)": last, "fill_price": fp, "fill_source": leg.get("fill_source"),
                "fill_price_estimated": bool(leg.get("fill_price_estimated")),
                "slippage_vs_last_pct": None if _pct(fp, last) is None else sign * _pct(fp, last),
                "slippage_vs_touch_pct": None if _pct(fp, touch) is None else sign * _pct(fp, touch),
                "slippage_vs_mid_pct": None if _pct(fp, market.get("mid")) is None else sign * _pct(fp, market.get("mid")),
                "requested_base_qty": leg.get("requested_qty"), "filled_base_qty": leg.get("filled_qty"),
                "notional_requested": body.notional,
                "notional_filled": (leg.get("filled_qty") or 0) * (fp or 0),
                "fee_actual_usdt": leg.get("fee_actual"),
                "fee_pct_of_notional": (None if not leg.get("fee_actual") or not fp or not leg.get("filled_qty")
                                        else 100 * leg["fee_actual"] / (leg["filled_qty"] * fp)),
                "position_entry_price": (_slim_position(pos_after[0]) or {}).get("entry_price") if pos_after else None,
                "leverage_requested": body.leverage,
                "leverage_reported_by_position": (_slim_position(pos_after[0]) or {}).get("leverage") if pos_after else None,
            },
        )
        st.setdefault("open", {})[symbol] = {
            "side": body.side, "leverage": body.leverage, "notional": body.notional, "opened_at": ev["started_at"],
            "leg": {k: v for k, v in leg.items() if k != "order_response"},
            "balance_before": bal_before, "market_before": market, "fee_rate_cfg": None,
        }
        _save_state(st)
        ev["ok"] = True
    except HTTPException:
        raise
    except Exception as e:
        log.exception("ordertest open failed")
        ev.update(ok=False, error=f"{e.__class__.__name__}: {e}", ended_at=_now())
        legs_info = getattr(e, "legs", None)
        if legs_info:
            ev["partial_legs"] = legs_info
    finally:
        await client.aclose()
    _write_event(ev)
    log.info("ordertest open %s %s ok=%s analysis=%s", ev.get("symbol"), body.side, ev.get("ok"),
             json.dumps(ev.get("analysis"), default=str)[:900])
    return ev


@router.post("/close")
async def close_position(body: CloseIn, db: Session = Depends(get_db)):
    from backend.engine.bot_engine import BotEngine
    if not body.confirm:
        raise HTTPException(400, "Tick the confirmation box: this places a REAL order on your XT account.")
    st = _load_state()
    client = _build_client(db, 1)
    try:
        await client._ensure_markets()
        symbol = client._normalize_symbol(body.symbol)
        rec = st.get("open", {}).get(symbol)
        if not rec:
            raise HTTPException(404, f"no test position recorded for {symbol}")
        backbone = config_service.get_section(db, "backbone")
        fee_cfg = float(backbone.get("fee_rate", 0.001) or 0.0)
        ev: dict[str, Any] = {"kind": "close", "started_at": _now(), "symbol": symbol, "request": body.model_dump()}
        try:
            entry_leg = rec["leg"]
            market = await _market_snapshot(client, symbol)
            bal_before = await _balance_snapshot(client)
            pos_before = await _positions_snapshot(client, symbol)
            last = market.get("last") or market.get("mid")
            if not last:
                raise HTTPException(502, f"no price for {symbol}: {market}")
            close_legs = close_legs_from_entry([entry_leg], {symbol: float(last)})
            t0 = time.perf_counter()
            placed = await BotEngine()._place_legs(client, close_legs, is_close=True, attempt_id="ordertest-x")
            order_ms = _ms(t0)
            cl = placed[0]
            fill = None
            # NOTE: close legs inherit the ENTRY order_id from close_legs_from_entry; only trust a
            # different id (fallback path) or the close response's own order.
            oid = (((cl.get("close_response") or {}).get("order") or {}).get("id")
                   or (cl.get("order_id") if cl.get("order_id") != entry_leg.get("order_id") else None))
            try:
                fill = await client.get_order_fill(oid, symbol, attempts=2, delay=0.3)
            except Exception as e:
                ev["fill_lookup_error"] = str(e)[:200]
            pos_after = await _positions_snapshot(client, symbol)
            market_after = await _market_snapshot(client, symbol)
            bal_after = await _balance_snapshot(client)

            side_close = cl.get("side")
            touch = market.get("ask") if side_close == "buy" else market.get("bid")
            sign = 1.0 if side_close == "buy" else -1.0
            fp = cl.get("fill_price")
            res = fills_pnl_and_fee([entry_leg], [cl], fee_cfg)
            gross, fee = (res if res else (None, None))
            entry_px = float(entry_leg.get("fill_price") or entry_leg.get("price") or 0)
            qty = float(entry_leg.get("filled_qty") or entry_leg.get("qty") or 0)
            sgn_pos = 1.0 if entry_leg.get("side") == "buy" else -1.0
            # what the bot used to report on XT: entry fill, exit at the decision price, fee at the config rate
            old_gross = sgn_pos * (float(last) - entry_px) * qty
            old_fee = fee_cfg * qty * (entry_px + float(last))
            b0, b1 = (rec.get("balance_before") or {}).get("total"), bal_after.get("total")
            still_open = [p for p in pos_after if not p.get("error") and float(p.get("liability") or 0) > 0]
            held = None
            try:
                held = (dt.datetime.fromisoformat(ev["started_at"].rstrip("Z")) -
                        dt.datetime.fromisoformat(rec["opened_at"].rstrip("Z"))).total_seconds()
            except Exception:
                pass
            ev.update(
                ended_at=_now(), order_total_ms=order_ms, held_seconds=held,
                before={"market": market, "balance": bal_before, "positions": [_slim_position(p) for p in pos_before]},
                after={"market": market_after, "balance": bal_after, "positions": [_slim_position(p) for p in pos_after]},
                leg={k: v for k, v in cl.items() if k not in ("order_response", "close_response")},
                close_response=cl.get("close_response"), order_response=cl.get("order_response"),
                exchange_fill={k: v for k, v in (fill or {}).items() if k != "raw"} or None,
                exchange_fill_raw=(fill or {}).get("raw"),
                analysis={
                    "decision_price(last)": last, "exit_fill_price": fp, "fill_source": cl.get("fill_source"),
                    "fill_price_estimated": bool(cl.get("fill_price_estimated")),
                    "slippage_vs_last_pct": None if _pct(fp, last) is None else sign * _pct(fp, last),
                    "slippage_vs_touch_pct": None if _pct(fp, touch) is None else sign * _pct(fp, touch),
                    "slippage_vs_mid_pct": None if _pct(fp, market.get("mid")) is None else sign * _pct(fp, market.get("mid")),
                    "closed_base_qty": cl.get("filled_qty"), "entry_base_qty": qty,
                    "fee_actual_usdt": cl.get("fee_actual"),
                    "position_fully_closed": not still_open,
                },
                summary={
                    "entry_fill_price": entry_px, "exit_fill_price": fp,
                    "gross_pnl_from_fills": gross, "fees_used_for_pnl": fee,
                    "exchange_close_profit_gross": (fill or {}).get("close_profit"),
                    "exchange_entry_price_precise": (fill or {}).get("open_price"),
                    "fees_are_actual": bool(entry_leg.get("fee_actual") is not None and cl.get("fee_actual") is not None),
                    "net_pnl_from_fills": None if gross is None else gross - fee,
                    "bot_old_estimate_net_pnl": old_gross - old_fee,
                    "bot_old_estimate_note": f"entry fill, exit at decision price, fee_rate={fee_cfg}",
                    "wallet_balance_before_open": b0, "wallet_balance_after_close": b1,
                    "wallet_balance_change": (None if b0 is None or b1 is None else float(b1) - float(b0)),
                    "wallet_note": "ground truth, but the balance endpoint can lag a few seconds",
                },
                ok=True,
            )
            if not still_open:
                st["open"].pop(symbol, None)
                _save_state(st)
        except HTTPException:
            raise
        except Exception as e:
            log.exception("ordertest close failed")
            ev.update(ok=False, error=f"{e.__class__.__name__}: {e}", ended_at=_now())
            if getattr(e, "legs", None):
                ev["partial_legs"] = e.legs
    finally:
        await client.aclose()
    _write_event(ev)
    log.info("ordertest close %s ok=%s summary=%s", ev.get("symbol"), ev.get("ok"),
             json.dumps(ev.get("summary"), default=str)[:900])
    return ev
