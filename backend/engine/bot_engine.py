"""
Background trading loop. Raw-price OLS residual; beta share ratios.

Paper: cash PnL from residual model (qty_y * ΔR − fees).
Live: PnL from Nobitex position.PNL after close (account data).

Safety (audit-driven):
  - Pre-trade margin balance check (live)
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
import traceback
import uuid

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
from backend.strategy.zscore import decide_entry, decide_exit
from backend.strategy.sizing import (
    leg_orders,
    close_legs_from_entry,
    MIN_ORDER_VALUE_IRT,
    total_required_collateral,
    assert_same_quote,
)
from backend.strategy.pnl import residual_cash_pnl, legs_gross_notional

log = logging.getLogger("bot_engine")

# Refuse entry if required collateral exceeds this fraction of free balance.
BALANCE_SAFETY_FRACTION = 0.90


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


class BotEngine:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None
        self._group_errors: dict[int, str] = {}

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

        # Active groups always. Also manage inactive groups that still have open trades
        # so deactivating a group does not orphan live risk (audit #13).
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
                try:
                    await self._process_group(
                        db, group, backbone, md_client, trading_client,
                        resolution, trading_mode, state.is_running,
                        allow_new_entries=(group.status == "active"),
                    )
                    self._group_errors.pop(group.id, None)
                except Exception:
                    err = traceback.format_exc()
                    self._last_error = err
                    self._group_errors[group.id] = err
                    log.exception("group %s failed this cycle", group.id)
        finally:
            await md_client.aclose()
            if not isinstance(trading_client, PaperExchangeClient):
                await trading_client.aclose()

    async def _process_group(
        self, db, group: Group, backbone, md_client, trading_client,
        resolution, trading_mode, is_running: bool,
        allow_new_entries: bool = True,
    ):
        try:
            assert_same_quote(list(group.symbols or []), group.dependent_symbol)
        except ValueError as e:
            log.error("group %s currency mismatch: %s", group.id, e)
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
            # Always manage exits — even if bot is stopped (audit #6).
            await self._check_exit(
                db, group, open_trade, latest_prices, backbone_local, trading_client, trading_mode,
            )
        elif is_running and allow_new_entries:
            await self._check_entry(
                db, group, price_df, latest_prices, backbone_local, trading_client, trading_mode,
            )

    async def _pretrade_balance_ok(
        self, trading_client, legs: list[dict], trading_mode: str,
    ) -> tuple[bool, str]:
        if trading_mode != "live" or not isinstance(trading_client, NobitexClient):
            return True, ""
        required = total_required_collateral(legs, leverage=1.0)
        free = await trading_client.get_margin_active_balance_irt()
        if free is None:
            log.warning("could not read margin wallet balance — proceeding with caution")
            return True, ""
        limit = free * BALANCE_SAFETY_FRACTION
        if required > limit:
            msg = (
                f"insufficient margin: need ~{required:.0f} IRT collateral "
                f"but free active balance={free:.0f} (safety {BALANCE_SAFETY_FRACTION:.0%})"
            )
            return False, msg
        return True, ""

    async def _place_legs(
        self,
        trading_client,
        legs: list[dict],
        *,
        is_close: bool = False,
        attempt_id: str | None = None,
    ) -> list[dict]:
        """
        Place or close legs sequentially. Never raises mid-basket without
        attempting to unwind already-filled entry legs (audit #2).
        """
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

            # -------- CLOSE path --------
            if is_close and isinstance(trading_client, NobitexClient) and leg.get("position_id"):
                close_qty = qty
                already_done = False
                try:
                    pos = await trading_client.get_position(int(leg["position_id"]))
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
                        int(leg["position_id"]),
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
                    out.append(leg)
                    continue
                except NobitexError as e:
                    # Only fall back to opposite order if liability still open.
                    liab_now = None
                    try:
                        pos_now = await trading_client.get_position(int(leg["position_id"]))
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

            # -------- OPEN / fallback opposite order --------
            try:
                kwargs = {"price": None, "client_order_id": coid}
                if isinstance(trading_client, NobitexClient):
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
                # On close path: record failure, continue other legs
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

            if not is_close and isinstance(trading_client, NobitexClient) and order_id:
                try:
                    await asyncio.sleep(0.6)
                    pid = await trading_client.resolve_position_id(
                        symbol, side, opened_after_iso=opened_after,
                    )
                    if pid:
                        leg["position_id"] = pid
                except Exception as e:
                    log.debug("resolve_position_id failed: %s", e)

            out.append(leg)
            if not is_close:
                filled_entry_indices.append(len(out) - 1)

        # Entry: if any leg marked failed (shouldn't reach here without raise)
        if not is_close and any(l.get("failed") for l in out):
            raise PartialLegsError("partial entry basket", out)

        return out

    async def _rollback_filled_entries(
        self, trading_client, legs: list[dict], indices: list[int],
    ):
        """Best-effort reverse of successfully opened entry legs."""
        log.error("rolling back %d filled entry leg(s)", len(indices))
        for idx in reversed(indices):
            leg = legs[idx]
            if leg.get("failed") or leg.get("rolled_back"):
                continue
            try:
                if isinstance(trading_client, NobitexClient) and leg.get("position_id"):
                    qty = float(leg.get("filled_qty") or leg["qty"])
                    try:
                        pos = await trading_client.get_position(int(leg["position_id"]))
                        liab = _parse_money(pos.get("liability"))
                        if liab is not None and liab > 0:
                            qty = liab
                    except Exception:
                        pass
                    await trading_client.close_position(
                        int(leg["position_id"]), amount=qty, execution="market",
                        client_order_id=_client_order_id("rb", leg["symbol"], "x"),
                    )
                else:
                    opp = "sell" if leg["side"] == "buy" else "buy"
                    qty = float(leg.get("filled_qty") or leg["qty"])
                    kwargs = {"price": None}
                    if isinstance(trading_client, NobitexClient):
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
            int(l["position_id"]): l
            for l in (legs_close or [])
            if l.get("position_id") is not None
        }

        for leg in legs_entry or []:
            pid = leg.get("position_id")
            if pid is None:
                details.append({"symbol": leg.get("symbol"), "error": "no position_id"})
                continue
            pid = int(pid)
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
                    pos = next((p for p in past if int(p.get("id", -1)) == pid), None)
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

    async def _check_exit(self, db, group, open_trade: Trade, latest_prices, backbone, trading_client, trading_mode):
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
            # Keep trade open for next cycle; do not mark closed.
            self._last_error = str(e)
            self._group_errors[group.id] = f"exit failed: {e}"
            log.error("group %s trade #%s exit place failed: %s", group.id, open_trade.id, e)
            return

        # If any close leg still failed and not already_closed, keep open.
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
        exchange_details = None
        if trading_mode == "live" and isinstance(trading_client, NobitexClient):
            try:
                realized, exchange_details = await self._realized_pnl_from_exchange(
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
    ):
        window = price_df.iloc[-int(backbone["window_size"]):]
        price_matrix = {s: window[s].to_numpy() for s in group.symbols}
        try:
            fit_res = fit_ols(group.dependent_symbol, price_matrix)
        except Exception:
            return

        stat = test_stationarity(fit_res.residual, backbone["adf_alpha"], backbone["kpss_alpha"])
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

        if not stat.passed or fit_res.resid_std == 0:
            return

        resid_now = float(fit_res.residual[-1])
        entry_dec = decide_entry(resid_now, fit_res.resid_mean, fit_res.resid_std, backbone["z_entry"])
        if not entry_dec.should_enter:
            return

        notional = float(backbone.get("trade_notional", 100) or 100)
        if notional <= 0:
            log.error("group %s: trade_notional <= 0 — skip entry", group.id)
            return
        dep = group.dependent_symbol
        if str(dep).upper().endswith(("IRT", "RLS")):
            notional = max(notional, MIN_ORDER_VALUE_IRT)

        try:
            legs = leg_orders(
                group.dependent_symbol,
                entry_dec.direction,
                fit_res.betas,
                latest_prices,
                notional,
            )
        except Exception as e:
            log.error("group %s sizing failed: %s", group.id, e)
            return

        ok, bal_msg = await self._pretrade_balance_ok(trading_client, legs, trading_mode)
        if not ok:
            self._last_error = bal_msg
            self._group_errors[group.id] = bal_msg
            log.error("group %s entry blocked: %s", group.id, bal_msg)
            return

        try:
            legs = await self._place_legs(
                trading_client, legs, is_close=False,
                attempt_id=f"e{group.id}",
            )
        except PartialLegsError as e:
            self._last_error = str(e)
            self._group_errors[group.id] = str(e)
            log.error("group %s partial entry (rolled back if possible): %s", group.id, e)
            # Persist a failed_partial trade for audit visibility
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
