"""
Background trading loop. Raw-price OLS residual; beta share ratios.

Paper: cash PnL from residual model (qty_y * ΔR − fees).
Live: PnL from Nobitex position.PNL after close (account data).

Safety (audit-driven):
  - Pre-trade margin balance check (live) — refuse if balance unreadable
  - Scale trade_notional down to fit free balance (keep ≥50k IRT/leg)
  - Sequential legs with rollback of filled legs on partial failure
  - Close fallback only if position still has liability
  - Exit management runs even when bot is "stopped" (no new entries only)
  - clientOrderId for idempotency; opened_after for position resolution
  - Store filled_qty when available
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
import traceback
import uuid

from sqlalchemy.orm.exc import ObjectDeletedError

from backend.db import SessionLocal
from backend import config_service
from backend.models import Group, Trade, OLSFit, BotState
from backend.bot_state_service import get_or_create_bot_state
from backend.exchange import factory
from backend.exchange.paper import PaperExchangeClient
from backend.exchange.nobitex import NobitexClient, NobitexError
from backend.utils import seconds_to_resolution, fetch_price_df
from backend.strategy.ols import fit_ols, residual_from_frozen_fit
from backend.strategy.stats_tests import test_stationarity
from backend.strategy.zscore import decide_entry, decide_exit, ExitDecision
from backend.strategy.sizing import (
    leg_orders,
    close_legs_from_entry,
    MIN_ORDER_VALUE_IRT,
    total_required_collateral,
    assert_same_quote,
    assert_same_exchange,
    ExcessiveScalingError,
)
from backend.strategy.pnl import residual_cash_pnl, legs_gross_notional, entry_target_check
from backend.engine import xt_hooks
try:
    from backend.exchange.xt import XTClient, XTError
except ImportError:
    XTClient = None  # type: ignore
    XTError = RuntimeError  # type: ignore
try:
    from ccxt.base.errors import RateLimitExceeded as CcxtRateLimitExceeded
except ImportError:
    CcxtRateLimitExceeded = type("CcxtRateLimitExceeded", (Exception,), {})

log = logging.getLogger("bot_engine")

# Refuse entry if required collateral exceeds this fraction of free balance.
BALANCE_SAFETY_FRACTION = 0.85
# Extra headroom for fees / mark-price drift between sizing and fill.
BALANCE_BUFFER_IRT = 20_000.0


def _parse_money(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "")
    s = s.replace("\u2212", "-").replace("−", "-")
    s = re.sub(r"[^0-9.+\-eE]", "", s)
    if not s or s in ("+", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None



def _pos_id(raw):
    """Nobitex uses int ids; XT may use symbol strings."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw)
    try:
        return int(s)
    except (TypeError, ValueError):
        return s


def _client_order_id(prefix: str, symbol: str, side: str) -> str:
    """Nobitex clientOrderId max ~32 chars; keep unique and short."""
    tag = uuid.uuid4().hex[:10]
    sym = re.sub(r"[^A-Za-z0-9]", "", symbol)[:8]
    return f"{prefix[:4]}{sym}{side[:1]}{tag}"[:32]


class PartialLegsError(RuntimeError):
    """Raised after partial basket placement when rollback was attempted."""

    def __init__(self, message: str, legs: list[dict]):
        super().__init__(message)
        self.legs = legs



def _fill_price_from_position(pos: dict | None, *, is_close: bool = False) -> float | None:
    """Extract Rial fill price from a Nobitex position payload."""
    if not pos:
        return None
    key = "exitPrice" if is_close else "entryPrice"
    # For closes never fall back to entryPrice: it would report the OPEN price as
    # the exit fill. markPrice is only an estimate; callers flag it as such.
    for k in ((key, "markPrice") if is_close else (key, "markPrice")):
        v = _parse_money(pos.get(k))
        if v is not None and v > 0:
            return float(v)
    return None


class BotEngine:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._group_errors: dict[int, str] = {}
        self._pause_until: str | None = None  # ISO timestamp; XT rate-limit lockout
        self._entry_cooldown_until: dict[int, float] = {}   # group_id -> monotonic deadline
        self._last_fit_logged: dict[int, float] = {}        # group_id -> monotonic time

    def start_background_loop(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop_forever())

    async def _loop_forever(self):
        while True:
            db = SessionLocal()
            try:
                state = get_or_create_bot_state(db)
                await self._run_cycle(db, state)
                sampling_time = config_service.get_section(db, "backbone").get("sampling_time", 60)
            except Exception:
                self._last_error = traceback.format_exc()
                log.exception("bot cycle failed")
                sampling_time = 30
            finally:
                db.close()
            await asyncio.sleep(max(1, int(sampling_time)))

    async def _run_cycle(self, db, state: BotState):
        backbone = config_service.get_section(db, "backbone")
        trading_mode = state.trading_mode or "paper"
        exchange = (getattr(state, "exchange", None) or "nobitex").strip().lower()

        if xt_hooks.is_paused(self):
            log.warning("bot paused until %s (rate-limit lockout) — skipping cycle entries", self._pause_until)
            # Still process exits only: fall through with is_running forced false for entries
            # by temporarily treating is_running as False below via allow_new_entries.

        active = db.query(Group).filter_by(status="active").all()
        active_ids = {g.id for g in active}
        open_trade_group_ids = {
            tid for (tid,) in db.query(Trade.group_id).filter_by(
                status="open", mode=trading_mode,
            ).distinct().all()
        }
        orphan_ids = open_trade_group_ids - active_ids
        extra = []
        if orphan_ids:
            extra = db.query(Group).filter(Group.id.in_(orphan_ids)).all()
            log.warning(
                "managing open trades on non-active groups: %s",
                [g.id for g in extra],
            )
        groups = list(active) + list(extra)
        if not groups:
            return

        md_client = factory.build_market_data_client(db)
        try:
            trading_client = factory.build_trading_client(db)
        except Exception as e:
            self._last_error = str(e)
            log.error("trading client unavailable: %s", e)
            await md_client.aclose()
            return

        resolution = seconds_to_resolution(backbone["sampling_time"])

        try:
            for group in groups:
                gid = group.id
                try:
                    status = group.status
                    allow_entries = (status == "active") and (not xt_hooks.is_paused(self))
                    await self._process_group(
                        db, group, backbone, md_client, trading_client,
                        resolution, trading_mode, state.is_running,
                        allow_new_entries=allow_entries,
                        exchange=exchange,
                    )
                    self._group_errors.pop(gid, None)
                except ObjectDeletedError:
                    log.warning("group %s was deleted during cycle — skipping", gid)
                    self._group_errors.pop(gid, None)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                except Exception as cycle_exc:
                    if xt_hooks.handle_rate_limit(self, cycle_exc, exchange=exchange):
                        self._last_error = f"XT rate-limit pause until {self._pause_until}"
                        log.error("account-wide rate limit — stopping group loop this cycle")
                        break
                    err = traceback.format_exc()
                    self._last_error = err
                    self._group_errors[gid] = err
                    log.exception("group %s failed this cycle", gid)
        finally:
            await md_client.aclose()
            if not isinstance(trading_client, PaperExchangeClient):
                await trading_client.aclose()

    async def _process_group(
        self, db, group: Group, backbone, md_client, trading_client,
        resolution, trading_mode, is_running: bool,
        allow_new_entries: bool = True,
        exchange: str = "nobitex",
    ):
        group_ex = (getattr(group, "exchange", None) or exchange or "nobitex").strip().lower()
        try:
            assert_same_exchange(list(group.symbols or []), group_ex, group.dependent_symbol)
        except ValueError as e:
            log.error("group %s currency/exchange mismatch: %s", group.id, e)
            self._group_errors[group.id] = str(e)
            return

        configured_window = int(backbone["window_size"])
        bars_needed = configured_window + 5
        price_df = await fetch_price_df(md_client, group.symbols, resolution, bars_needed)

        min_bars = 30
        if price_df.empty or len(price_df) < min_bars:
            log.warning(
                "group %s: insufficient OHLC (got %d bars, need >= %d)",
                group.id, 0 if price_df.empty else len(price_df), min_bars,
            )
            return

        effective_window = min(configured_window, len(price_df) - 1)
        if effective_window < min_bars:
            log.warning("group %s: effective window %d too small", group.id, effective_window)
            return

        backbone_local = dict(backbone)
        backbone_local["window_size"] = effective_window
        latest_prices = price_df.iloc[-1].to_dict()

        open_trade = (
            db.query(Trade)
            .filter_by(group_id=group.id, status="open", mode=trading_mode)
            .first()
        )

        if open_trade is not None:
            await self._check_exit(
                db, group, open_trade, latest_prices, backbone_local, trading_client, trading_mode,
                exchange=group_ex,
            )
        elif is_running and allow_new_entries:
            await self._check_entry(
                db, group, price_df, latest_prices, backbone_local, trading_client, trading_mode,
                exchange=group_ex,
            )

    def _start_cooldown(self, group_id: int, backbone: dict) -> None:
        sec = float(backbone.get("entry_retry_cooldown_sec", 600) or 0)
        if sec > 0:
            self._entry_cooldown_until[group_id] = time.monotonic() + sec

    def _in_cooldown(self, group_id: int) -> bool:
        return time.monotonic() < self._entry_cooldown_until.get(group_id, 0.0)

    @staticmethod
    def _symbols_in_use(db, trading_mode: str, exclude_group_id: int) -> set[str]:
        used: set[str] = set()
        for t in db.query(Trade).filter(
            Trade.status.in_(("open",)), Trade.mode == trading_mode,
            Trade.group_id != exclude_group_id,
        ).all():
            for leg in (t.legs_entry or []):
                if leg.get("symbol"):
                    used.add(leg["symbol"])
        return used

    async def _read_free_balance_irt(self, trading_client, exchange: str = "nobitex") -> float | None:
        """Legacy name; quote is IRT for nobitex, USDT for xt."""
        return await xt_hooks.read_free_balance(trading_client, exchange=exchange)

    async def _fit_notional_to_balance(
        self,
        trading_client,
        *,
        dependent_symbol: str,
        direction: str,
        betas: dict,
        prices: dict,
        desired_notional: float,
        trading_mode: str,
        max_scale: float | None = None,
        exchange: str = "nobitex",
        symbols: list[str] | None = None,
    ) -> tuple[list[dict] | None, float, str]:
        """
        Build legs for desired_notional. On live, scale down so total collateral
        fits free balance × safety − buffer, while every leg stays ≥ exchange min.

        Returns (legs | None, final_notional, message).
        """
        min_val = await xt_hooks.resolve_min_order_value(
            trading_client, symbols or [dependent_symbol],
            exchange=exchange, default_irt=MIN_ORDER_VALUE_IRT,
        )
        buffer = BALANCE_BUFFER_IRT if exchange == "nobitex" else max(1.0, min_val * 0.1)

        def _build(n: float) -> list[dict]:
            return leg_orders(
                dependent_symbol, direction, betas, prices, float(n),
                min_order_value=min_val,
                max_scale=max_scale,
            )

        if trading_mode != "live" or not (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))):
            legs = _build(desired_notional)
            return legs, float(desired_notional), ""

        free = await self._read_free_balance_irt(trading_client, exchange=exchange)
        if free is None:
            return None, 0.0, (
                "cannot read margin wallet balance — refusing live entry "
                "(API key needs wallet read; endpoint /users/wallets/list)"
            )

        usable = max(0.0, float(free) * BALANCE_SAFETY_FRACTION - buffer)
        if usable < min_val * 2:
            return None, 0.0, (
                f"free balance too low: free={free:.0f} IRT, usable≈{usable:.0f} "
                f"(need ≥ {min_val * 2:.0f} for a 2-leg basket)"
            )

        try:
            legs = _build(desired_notional)
        except Exception as e:
            return None, 0.0, f"sizing failed: {e}"
        need = total_required_collateral(legs, leverage=1.0)
        notional = float(desired_notional)

        if need > usable:
            if need <= 0:
                return None, 0.0, "zero required collateral?"
            scale = (usable / need) * 0.98
            notional = float(desired_notional) * scale
            if notional < min_val:
                return None, 0.0, (
                    f"insufficient margin: desired need≈{need:.0f} IRT for "
                    f"notional={desired_notional:.0f}, free={free:.0f}, usable={usable:.0f}"
                )
            try:
                legs = _build(notional)
            except Exception as e:
                return None, 0.0, f"sizing failed after scale: {e}"
            need = total_required_collateral(legs, leverage=1.0)
            if need > usable:
                scale2 = (usable / need) * 0.98
                notional = notional * scale2
                if notional < min_val:
                    return None, 0.0, (
                        f"insufficient margin after min-order scale-up: need≈{need:.0f}, "
                        f"usable={usable:.0f}, free={free:.0f}"
                    )
                legs = _build(notional)
                need = total_required_collateral(legs, leverage=1.0)
                if need > usable:
                    return None, 0.0, (
                        f"insufficient margin: need≈{need:.0f} IRT, free={free:.0f}, "
                        f"usable={usable:.0f}"
                    )

        msg = ""
        if notional + 1 < float(desired_notional):
            msg = (
                f"scaled notional {desired_notional:.0f} → {notional:.0f} IRT "
                f"(collateral need={need:.0f}, free={free:.0f}, usable={usable:.0f})"
            )
            log.warning("%s", msg)
        else:
            log.info(
                "pretrade OK notional=%.0f need=%.0f free=%.0f usable=%.0f",
                notional, need, free, usable,
            )
        return legs, notional, msg

    async def _place_legs(
        self,
        trading_client,
        legs: list[dict],
        *,
        is_close: bool = False,
        attempt_id: str | None = None,
    ) -> list[dict]:
        out: list[dict] = []
        attempt_id = attempt_id or uuid.uuid4().hex[:8]
        filled_entry_indices: list[int] = []

        for i, leg in enumerate(legs):
            if i > 0:
                await asyncio.sleep(0.35)

            symbol = leg["symbol"]
            side = leg["side"]
            qty = float(leg.get("filled_qty") if leg.get("filled_qty") is not None else leg["qty"])
            ref_price = leg.get("price")
            coid = _client_order_id("c" if is_close else "o", symbol, side)

            if is_close and (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))) and leg.get("position_id"):
                close_qty = qty
                already_done = False
                try:
                    pos = await trading_client.get_position(_pos_id(leg["position_id"]))
                    liab = _parse_money(pos.get("liability"))
                    st = str(pos.get("status") or "").lower()
                    if st in ("closed", "liquidated", "expired") or (liab is not None and liab <= 0):
                        already_done = True
                    elif liab is not None and liab > 0:
                        close_qty = liab
                    leg["pre_close_position"] = pos
                except Exception as e:
                    log.debug("pre-close position fetch failed: %s", e)

                if already_done:
                    leg = {
                        **leg,
                        "already_closed": True,
                        "closed_via": "already_closed",
                        "filled_qty": 0.0,
                    }
                    out.append(leg)
                    continue

                try:
                    resp = await trading_client.close_position(
                        _pos_id(leg["position_id"]),
                        amount=close_qty,
                        execution="market",
                        client_order_id=coid,
                    )
                    leg = {
                        **leg,
                        "close_response": resp,
                        "closed_via": "position_close",
                        "close_qty": close_qty,
                        "client_order_id": coid,
                        "filled_qty": close_qty,
                    }
                    # Poll position for actual exit/fill price (Rial).
                    decision_px = float(leg.get("price") or 0) or None
                    fill = None
                    try:
                        for attempt in range(3):
                            await asyncio.sleep(0.4 if attempt else 0.2)
                            pos_after = await trading_client.get_position(_pos_id(leg["position_id"]))
                            fill = _fill_price_from_position(pos_after, is_close=True)
                            if fill is not None:
                                break
                    except Exception as e:
                        log.debug("post-close fill_price poll failed: %s", e)
                    if fill is not None:
                        leg["decision_price"] = decision_px
                        leg["fill_price"] = fill
                        if decision_px and decision_px > 0:
                            slip = (fill - decision_px) / decision_px
                            log.info(
                                "close leg %s fill_price=%.4g decision_price=%.4g slippage=%.4f",
                                symbol, fill, decision_px, slip,
                            )
                    else:
                        leg["decision_price"] = decision_px
                        leg["fill_price"] = decision_px
                        leg["fill_price_estimated"] = True
                    out.append(leg)
                    continue
                except (NobitexError, XTError) as e:
                    liab_now = None
                    try:
                        pos_now = await trading_client.get_position(_pos_id(leg["position_id"]))
                        liab_now = _parse_money(pos_now.get("liability"))
                        st = str(pos_now.get("status") or "").lower()
                        if st in ("closed", "liquidated", "expired") or (liab_now is not None and liab_now <= 0):
                            leg = {
                                **leg,
                                "already_closed": True,
                                "closed_via": "already_closed_after_error",
                                "close_error": str(e),
                                "filled_qty": 0.0,
                            }
                            out.append(leg)
                            continue
                    except Exception:
                        pass

                    if liab_now is not None and liab_now <= 0:
                        leg = {
                            **leg,
                            "already_closed": True,
                            "close_error": str(e),
                            "filled_qty": 0.0,
                        }
                        out.append(leg)
                        continue

                    log.warning(
                        "position close failed id=%s liab=%s: %s — opposite order fallback",
                        leg.get("position_id"), liab_now, e,
                    )

            try:
                kwargs = {"price": None, "client_order_id": coid}
                if (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))):
                    kwargs["ref_price"] = ref_price
                opened_after = dt.datetime.utcnow().isoformat()
                resp = await trading_client.place_order(symbol, side, qty, **kwargs)
            except Exception as e:
                leg = {**leg, "failed": True, "error": str(e), "client_order_id": coid}
                out.append(leg)
                if not is_close and filled_entry_indices:
                    await self._rollback_filled_entries(trading_client, out, filled_entry_indices)
                    raise PartialLegsError(
                        f"leg {symbol} failed after partial fills: {e}", out,
                    ) from e
                if not is_close:
                    raise PartialLegsError(f"entry leg {symbol} failed: {e}", out) from e
                log.error("close leg %s failed: %s", symbol, e)
                continue

            order = (resp or {}).get("order") or {}
            order_id = order.get("id") or (resp or {}).get("id")
            matched = _parse_money(order.get("matchedAmount")) or _parse_money(
                (resp or {}).get("matchedAmount")
            )
            filled_qty = float(matched) if matched is not None and matched > 0 else qty

            leg = {
                **leg,
                "order_response": resp,
                "order_id": order_id,
                "client_order_id": coid,
                "filled_qty": filled_qty,
                "requested_qty": qty,
            }

            if not is_close and (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))) and order_id:
                try:
                    await asyncio.sleep(0.6)
                    pid = await trading_client.resolve_position_id(
                        symbol, side, opened_after_iso=opened_after,
                    )
                    if pid:
                        leg["position_id"] = pid
                        # Capture real fill price (Rial) from position status.
                        fill = None
                        for attempt in range(3):
                            try:
                                pos = await trading_client.get_position(_pos_id(pid))
                                fill = _fill_price_from_position(pos, is_close=False)
                                if fill is not None:
                                    break
                            except Exception as e:
                                log.debug("get_position for fill_price failed: %s", e)
                            await asyncio.sleep(0.4)
                        decision_px = float(leg.get("price") or 0) or None
                        if fill is not None:
                            leg["decision_price"] = decision_px
                            leg["fill_price"] = fill
                            if decision_px and decision_px > 0:
                                slip = (fill - decision_px) / decision_px
                                log.info(
                                    "leg %s fill_price=%.4g decision_price=%.4g slippage=%.4f",
                                    symbol, fill, decision_px, slip,
                                )
                        else:
                            leg["decision_price"] = decision_px
                            leg["fill_price"] = decision_px
                            leg["fill_price_estimated"] = True
                            log.info(
                                "leg %s fill_price unavailable; using decision_price=%.4g",
                                symbol, decision_px or 0,
                            )
                except Exception as e:
                    log.debug("resolve_position_id failed: %s", e)

            # For closes via place_order fallback, try to capture fill similarly
            if is_close and (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))) and leg.get("position_id"):
                try:
                    pos = await trading_client.get_position(_pos_id(leg["position_id"]))
                    fill = _fill_price_from_position(pos, is_close=True)
                    decision_px = float(leg.get("price") or 0) or None
                    if fill is not None:
                        leg["decision_price"] = decision_px
                        leg["fill_price"] = fill
                        if decision_px and decision_px > 0:
                            slip = (fill - decision_px) / decision_px
                            log.info(
                                "close leg %s fill_price=%.4g decision_price=%.4g slippage=%.4f",
                                symbol, fill, decision_px, slip,
                            )
                    else:
                        leg["decision_price"] = decision_px
                        leg["fill_price"] = decision_px
                        leg["fill_price_estimated"] = True
                except Exception as e:
                    log.debug("close fill_price poll failed: %s", e)

            out.append(leg)
            if not is_close:
                filled_entry_indices.append(len(out) - 1)

        if not is_close and any(l.get("failed") for l in out):
            raise PartialLegsError("partial entry basket", out)

        return out

    async def _rollback_filled_entries(
        self, trading_client, legs: list[dict], indices: list[int],
    ):
        log.error("rolling back %d filled entry leg(s)", len(indices))
        for idx in reversed(indices):
            leg = legs[idx]
            if leg.get("failed") or leg.get("rolled_back"):
                continue
            try:
                if (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))) and leg.get("position_id"):
                    qty = float(leg.get("filled_qty") or leg["qty"])
                    try:
                        pos = await trading_client.get_position(_pos_id(leg["position_id"]))
                        liab = _parse_money(pos.get("liability"))
                        if liab is not None and liab > 0:
                            qty = liab
                    except Exception:
                        pass
                    await trading_client.close_position(
                        _pos_id(leg["position_id"]), amount=qty, execution="market",
                        client_order_id=_client_order_id("rb", leg["symbol"], "x"),
                    )
                else:
                    opp = "sell" if leg["side"] == "buy" else "buy"
                    qty = float(leg.get("filled_qty") or leg["qty"])
                    kwargs = {"price": None}
                    if (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))):
                        kwargs["ref_price"] = leg.get("price")
                        kwargs["client_order_id"] = _client_order_id("rb", leg["symbol"], opp)
                    await trading_client.place_order(leg["symbol"], opp, qty, **kwargs)
                legs[idx] = {**leg, "rolled_back": True}
                log.info("rolled back leg %s", leg.get("symbol"))
            except Exception as e:
                log.exception("FAILED to roll back leg %s: %s", leg.get("symbol"), e)
                legs[idx] = {**leg, "rollback_failed": True, "rollback_error": str(e)}

    async def _realized_pnl_from_exchange(
        self,
        trading_client: NobitexClient,
        legs_entry: list[dict],
        legs_close: list[dict],
    ) -> tuple[float | None, list[dict]]:
        details: list[dict] = []
        total = 0.0
        found = 0

        close_by_pid = {
            _pos_id(l["position_id"]): l
            for l in (legs_close or [])
            if l.get("position_id") is not None
        }

        for leg in legs_entry or []:
            pid = leg.get("position_id")
            if pid is None:
                details.append({"symbol": leg.get("symbol"), "error": "no position_id"})
                continue
            pid = _pos_id(pid)
            pos = None
            for attempt in range(8):
                await asyncio.sleep(0.7 if attempt else 0.3)
                try:
                    pos = await trading_client.get_position(pid)
                except Exception as e:
                    log.debug("get_position(%s) attempt %s: %s", pid, attempt, e)
                    pos = None
                if not pos:
                    continue
                st = str(pos.get("status") or "").lower()
                if st in ("closed", "liquidated", "expired") or pos.get("PNL") is not None:
                    break

            if not pos:
                try:
                    past = await trading_client.list_positions(status="past", page_size=50)
                    pos = next((p for p in past if str(p.get("id", -1)) == str(pid)), None)
                except Exception as e:
                    log.debug("past positions lookup failed: %s", e)

            if not pos:
                details.append({"symbol": leg.get("symbol"), "position_id": pid, "error": "not found"})
                continue

            pnl = _parse_money(pos.get("PNL"))
            if pnl is None:
                pnl = _parse_money(pos.get("unrealizedPNL"))

            entry = {
                "symbol": leg.get("symbol"),
                "position_id": pid,
                "status": pos.get("status"),
                "PNL": pos.get("PNL"),
                "parsed_pnl": pnl,
            }
            if pnl is not None:
                total += pnl
                found += 1
            details.append(entry)
            if pid in close_by_pid:
                close_by_pid[pid]["exchange_pnl"] = pnl

        if found == 0:
            return None, details
        return float(total), details

    async def _check_exit(self, db, group, open_trade: Trade, latest_prices, backbone, trading_client, trading_mode, exchange: str = "nobitex"):
        fit = open_trade.ols_fit
        if fit is None:
            log.error("trade #%s missing ols_fit — cannot exit", open_trade.id)
            return

        resid_now = residual_from_frozen_fit(
            group.dependent_symbol, latest_prices, fit.betas, fit.intercept,
        )
        exit_dec = decide_exit(
            resid_now, open_trade.direction, fit.resid_mean, fit.resid_std,
            backbone["z_close"], backbone["z_stop_loss"],
        )
        if not exit_dec.should_exit:
            max_hold_h = float(backbone.get("max_holding_hours", 0) or 0)
            if max_hold_h > 0 and open_trade.entry_time is not None:
                age_h = (dt.datetime.utcnow() - open_trade.entry_time).total_seconds() / 3600.0
                if age_h >= max_hold_h:
                    log.warning(
                        "group %s trade #%s time stop after %.1fh (z=%.2f)",
                        group.id, open_trade.id, age_h, exit_dec.z,
                    )
                    exit_dec = ExitDecision(True, "time_stop", exit_dec.z)
        if not exit_dec.should_exit:
            return

        cost_rate = float(backbone["fee_rate"]) + float(backbone["slippage_rate"])
        notional = float(
            open_trade.trade_notional
            if open_trade.trade_notional is not None
            else (backbone.get("trade_notional", 100) or 100)
        )

        legs_entry = open_trade.legs_entry
        if not legs_entry:
            legs_entry = leg_orders(
                group.dependent_symbol,
                open_trade.direction,
                fit.betas or {},
                open_trade.entry_prices or latest_prices,
                notional,
            )
        legs_close = close_legs_from_entry(legs_entry, latest_prices)

        try:
            legs_close = await self._place_legs(
                trading_client, legs_close, is_close=True,
                attempt_id=f"x{open_trade.id}",
            )
        except Exception as e:
            self._last_error = str(e)
            self._group_errors[group.id] = f"exit failed: {e}"
            log.error("group %s trade #%s exit place failed: %s", group.id, open_trade.id, e)
            return

        still_open = [
            l for l in legs_close
            if l.get("failed") and not l.get("already_closed")
        ]
        if still_open:
            open_trade.legs_close = legs_close
            db.commit()
            log.error(
                "group %s trade #%s partial close — keeping open; failed=%s",
                group.id, open_trade.id,
                [l.get("symbol") for l in still_open],
            )
            return

        qty_y = float(
            (legs_entry[0].get("filled_qty") or legs_entry[0]["qty"])
            if legs_entry else notional / max(
                float((open_trade.entry_prices or latest_prices).get(group.dependent_symbol, 1)), 1e-12
            )
        )
        gross = legs_gross_notional(legs_entry)
        model_pnl = residual_cash_pnl(
            open_trade.entry_residual, resid_now, open_trade.direction,
            qty_y, cost_rate, gross,
        )
        fee_model = cost_rate * 2.0 * gross

        realized = None
        if trading_mode == "live" and (isinstance(trading_client, NobitexClient) or (XTClient is not None and isinstance(trading_client, XTClient))):
            try:
                realized, _ = await self._realized_pnl_from_exchange(
                    trading_client, legs_entry, legs_close,
                )
            except Exception:
                log.exception("failed to read exchange PnL for trade #%s", open_trade.id)

        if realized is not None:
            reported_pnl = float(realized)
            open_trade.realized_pnl = float(realized)
            open_trade.pnl = float(realized)
            log.info(
                "group %s trade #%s LIVE PnL=%.6g (model %.6g)",
                group.id, open_trade.id, realized, model_pnl,
            )
        else:
            reported_pnl = float(model_pnl)
            open_trade.pnl = float(model_pnl)
            open_trade.realized_pnl = None

        open_trade.status = "closed"
        open_trade.close_time = dt.datetime.utcnow()
        open_trade.close_reason = exit_dec.reason
        open_trade.close_z = exit_dec.z
        open_trade.close_residual = resid_now
        open_trade.close_prices = latest_prices
        open_trade.legs_entry = legs_entry
        open_trade.legs_close = legs_close
        open_trade.model_pnl = float(model_pnl)
        open_trade.fee_paid = float(fee_model)
        if open_trade.trade_notional is None:
            open_trade.trade_notional = notional
        db.commit()

        log.info(
            "group %s trade #%s closed mode=%s reason=%s pnl=%.6g",
            group.id, open_trade.id, trading_mode, exit_dec.reason, reported_pnl,
        )

    async def _check_entry(
        self, db, group, price_df, latest_prices, backbone, trading_client, trading_mode,
        exchange: str = "nobitex",
    ):
        window = price_df.iloc[-int(backbone["window_size"]):]
        price_matrix = {s: window[s].to_numpy() for s in group.symbols}
        try:
            fit_res = fit_ols(group.dependent_symbol, price_matrix)
        except Exception:
            return

        if self._in_cooldown(group.id):
            return

        dep = group.dependent_symbol
        stat = test_stationarity(
            fit_res.residual, backbone["adf_alpha"], backbone["kpss_alpha"],
            y=price_matrix[dep],
            x_cols=[price_matrix[s] for s in price_matrix if s != dep],
            method=str(backbone.get("stationarity_method", "engle_granger")),
        )
        resid_now = float(fit_res.residual[-1])
        entry_dec = decide_entry(
            resid_now, fit_res.resid_mean, fit_res.resid_std, backbone["z_entry"],
            z_stop=float(backbone["z_stop_loss"]),
        )
        candidate = bool(stat.passed and fit_res.resid_std != 0 and entry_dec.should_enter)

        # Persist the fit only when it can become a trade, or at most every
        # fit_log_interval_sec (was: one row + residual_series JSON every cycle).
        interval = float(backbone.get("fit_log_interval_sec", 900) or 0)
        now_m = time.monotonic()
        due = interval <= 0 or (now_m - self._last_fit_logged.get(group.id, -1e18)) >= interval
        ols_row = None
        if candidate or due:
            ols_row = OLSFit(
                group_id=group.id,
                fitted_at=dt.datetime.utcnow(),
                window_start=window.index[0].to_pydatetime(),
                window_end=window.index[-1].to_pydatetime(),
                betas=fit_res.betas,
                intercept=fit_res.intercept,
                resid_mean=fit_res.resid_mean,
                resid_std=fit_res.resid_std,
                adf_stat=stat.adf_stat, adf_pvalue=stat.adf_pvalue,
                kpss_stat=stat.kpss_stat, kpss_pvalue=stat.kpss_pvalue,
                passed=stat.passed,
                residual_series=[[ts.isoformat(), float(v)] for ts, v in zip(window.index, fit_res.residual)],
            )
            db.add(ols_row)
            db.commit()
            db.refresh(ols_row)
            self._last_fit_logged[group.id] = now_m

        if not candidate:
            return

        notional = float(backbone.get("trade_notional", 100) or 100)
        if notional <= 0:
            log.error("group %s: trade_notional <= 0 — skip entry", group.id)
            return
        group_ex = (getattr(group, "exchange", None) or exchange or "nobitex").lower()
        if group_ex == "nobitex" and str(dep).upper().endswith(("IRT", "RLS")):
            notional = max(notional, MIN_ORDER_VALUE_IRT)

        if group_ex == "xt" and trading_mode == "live":
            # XT runs in one-way (net) position mode: a second group touching the
            # same symbol would merge into / net against the first group's
            # position (position id == symbol), corrupting both groups' PnL and
            # making one group's close flatten the other's hedge.
            clash = self._symbols_in_use(db, trading_mode, group.id) & set(group.symbols or [])
            if clash:
                log.info("group %s skip entry: XT symbol(s) %s already used by another open trade", group.id, sorted(clash))
                return

        # Profit-target entry gate (pure sizing, no I/O): skip if a perfect
        # close at z_close would not clear target_profit_rate of G after costs.
        try:
            preview_legs = leg_orders(
                group.dependent_symbol,
                entry_dec.direction,
                fit_res.betas,
                latest_prices,
                float(notional),
            )
            qty0 = float(preview_legs[0].get("qty") or 0) if preview_legs else 0.0
            if qty0 > 0:
                G_unit = legs_gross_notional(preview_legs) / qty0
            else:
                G_unit = 0.0
            # XT adds funding_rate_estimate * expected_holding_funding_intervals (default 0 for nobitex)
            cost_rate = xt_hooks.effective_cost_rate(
                backbone, exchange=getattr(group, "exchange", None) or "nobitex"
            )
            target_rate = float(backbone.get("target_profit_rate", 0.0) or 0.0)
            ok_gate, gate_det = entry_target_check(
                z_now=entry_dec.z,
                sigma=fit_res.resid_std,
                z_close=float(backbone["z_close"]),
                z_stop=float(backbone["z_stop_loss"]),
                gross_per_unit_y=G_unit,
                cost_rate=cost_rate,
                target_rate=target_rate,
            )
            if not ok_gate:
                log.info(
                    "group %s skip entry (profit gate): z_now=%.3f z_min=%s sigma_rel=%s G=%.4g reason=%s",
                    group.id,
                    entry_dec.z,
                    gate_det.get("z_min"),
                    gate_det.get("sigma_rel"),
                    G_unit,
                    gate_det.get("reject_reason"),
                )
                return
        except Exception as e:
            log.warning("group %s profit gate error (skipping entry, fail-closed): %s", group.id, e)
            return

        max_entry_scale = backbone.get("max_entry_scale")
        if max_entry_scale is not None:
            try:
                max_entry_scale = float(max_entry_scale)
                if max_entry_scale <= 0:
                    max_entry_scale = None
            except (TypeError, ValueError):
                max_entry_scale = None

        # Preview sizing against max_entry_scale before balance fit (same treatment as profit gate).
        try:
            leg_orders(
                group.dependent_symbol,
                entry_dec.direction,
                fit_res.betas,
                latest_prices,
                float(notional),
                max_scale=max_entry_scale,
            )
        except ExcessiveScalingError as e:
            log.info(
                "group %s skip entry (max_entry_scale): %s",
                group.id, e,
            )
            return

        legs, notional, bal_msg = await self._fit_notional_to_balance(
            trading_client,
            dependent_symbol=group.dependent_symbol,
            direction=entry_dec.direction,
            betas=fit_res.betas,
            prices=latest_prices,
            desired_notional=notional,
            trading_mode=trading_mode,
            max_scale=max_entry_scale,
            exchange=(getattr(group, "exchange", None) or exchange or "nobitex"),
            symbols=list(group.symbols or []),
        )
        if legs is None:
            self._last_error = bal_msg
            self._group_errors[group.id] = bal_msg
            log.error("group %s entry blocked: %s", group.id, bal_msg)
            self._start_cooldown(group.id, backbone)
            return
        if bal_msg:
            log.info("group %s: %s", group.id, bal_msg)

        try:
            legs = await self._place_legs(
                trading_client, legs, is_close=False,
                attempt_id=f"e{group.id}",
            )
        except PartialLegsError as e:
            self._last_error = str(e)
            self._group_errors[group.id] = str(e)
            log.error("group %s partial entry (rolled back if possible): %s", group.id, e)
            self._start_cooldown(group.id, backbone)
            trade = Trade(
                group_id=group.id,
                ols_fit_id=ols_row.id,
                direction=entry_dec.direction,
                mode=trading_mode,
                entry_time=dt.datetime.utcnow(),
                entry_z=entry_dec.z,
                entry_residual=resid_now,
                entry_prices=latest_prices,
                status="failed_partial",
                legs_entry=e.legs,
                trade_notional=notional,
                close_reason="entry_rollback",
                close_time=dt.datetime.utcnow(),
            )
            db.add(trade)
            db.commit()
            return
        except Exception as e:
            self._last_error = str(e)
            self._group_errors[group.id] = str(e)
            log.error("group %s entry order failed: %s", group.id, e)
            self._start_cooldown(group.id, backbone)
            return

        trade = Trade(
            group_id=group.id,
            ols_fit_id=ols_row.id,
            direction=entry_dec.direction,
            mode=trading_mode,
            entry_time=dt.datetime.utcnow(),
            entry_z=entry_dec.z,
            entry_residual=resid_now,
            entry_prices=latest_prices,
            status="open",
            legs_entry=legs,
            trade_notional=notional,
        )
        db.add(trade)
        db.commit()
        log.info(
            "group %s opened %s mode=%s notional=%.4g legs=%s",
            group.id, entry_dec.direction, trading_mode, notional,
            [
                (l["symbol"], l["side"], round(float(l.get("filled_qty") or l["qty"]), 8),
                 l.get("position_id"))
                for l in legs
            ],
        )


bot_engine = BotEngine()