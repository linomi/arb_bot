"""
Background trading loop. Raw-price OLS residual; beta share ratios.

Paper: cash PnL from residual model (qty_y * ΔR − fees).
Live: PnL from Nobitex position.PNL after close (account data).
"""
import asyncio
import datetime as dt
import logging
import re
import traceback

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
from backend.strategy.sizing import leg_orders, close_legs_from_entry, MIN_ORDER_VALUE_IRT
from backend.strategy.pnl import residual_cash_pnl, legs_gross_notional

log = logging.getLogger("bot_engine")


def _parse_money(val) -> float | None:
    """Parse Nobitex string numbers; handle unicode minus."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "")
    s = s.replace("\u2212", "-").replace("−", "-")  # unicode minus
    s = re.sub(r"[^0-9.+\-eE]", "", s)
    if not s or s in ("+", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class BotEngine:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None

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
        groups = db.query(Group).filter_by(status="active").all()
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
                        resolution, state.trading_mode, state.is_running,
                    )
                except Exception:
                    self._last_error = traceback.format_exc()
                    log.exception("group %s failed this cycle", group.id)
        finally:
            await md_client.aclose()
            if not isinstance(trading_client, PaperExchangeClient):
                await trading_client.aclose()

    async def _process_group(
        self, db, group: Group, backbone, md_client, trading_client,
        resolution, trading_mode, is_running: bool,
    ):
        configured_window = int(backbone["window_size"])
        bars_needed = configured_window + 5
        price_df = await fetch_price_df(md_client, group.symbols, resolution, bars_needed)

        min_bars = 30
        if price_df.empty or len(price_df) < min_bars:
            log.warning(
                "group %s: insufficient OHLC (got %d bars, need >= %d) symbols=%s resolution=%s",
                group.id, 0 if price_df.empty else len(price_df), min_bars, group.symbols, resolution,
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
            if is_running:
                await self._check_exit(
                    db, group, open_trade, latest_prices, backbone_local, trading_client, trading_mode,
                )
        else:
            await self._check_entry(
                db, group, price_df, latest_prices, backbone_local, trading_client, trading_mode, is_running,
            )

    async def _place_legs(self, trading_client, legs: list[dict], *, is_close: bool = False) -> list[dict]:
        out = []
        for i, leg in enumerate(legs):
            if i > 0:
                await asyncio.sleep(0.35)

            symbol = leg["symbol"]
            side = leg["side"]
            qty = float(leg["qty"])

            if is_close and isinstance(trading_client, NobitexClient) and leg.get("position_id"):
                # Prefer full remaining liability when available (docs: final settle).
                close_qty = qty
                try:
                    pos = await trading_client.get_position(int(leg["position_id"]))
                    liab = _parse_money(pos.get("liability"))
                    if liab is not None and liab > 0:
                        close_qty = liab
                    leg["pre_close_position"] = pos
                except Exception as e:
                    log.debug("pre-close position fetch failed: %s", e)

                try:
                    resp = await trading_client.close_position(
                        int(leg["position_id"]),
                        amount=close_qty,
                        execution="market",
                    )
                    leg = {**leg, "close_response": resp, "closed_via": "position_close", "close_qty": close_qty}
                    out.append(leg)
                    continue
                except NobitexError as e:
                    log.warning(
                        "position close failed id=%s: %s — falling back to opposite order",
                        leg.get("position_id"), e,
                    )

            resp = await trading_client.place_order(symbol, side, qty, price=None)
            order = (resp or {}).get("order") or {}
            order_id = order.get("id") or (resp or {}).get("id")
            leg = {**leg, "order_response": resp, "order_id": order_id}

            if not is_close and isinstance(trading_client, NobitexClient) and order_id:
                try:
                    await asyncio.sleep(0.6)
                    pid = await trading_client.resolve_position_id(symbol, side)
                    if pid:
                        leg["position_id"] = pid
                except Exception as e:
                    log.debug("resolve_position_id failed: %s", e)

            out.append(leg)
        return out

    async def _realized_pnl_from_exchange(
        self,
        trading_client: NobitexClient,
        legs_entry: list[dict],
        legs_close: list[dict],
    ) -> tuple[float | None, list[dict]]:
        """
        Sum position.PNL from Nobitex after close (dst-currency units, usually IRT/RLS).
        Returns (total_pnl, per_leg details). None total if no position data found.
        """
        details: list[dict] = []
        total = 0.0
        found = 0

        # Build map position_id -> close leg for annotation
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
            # Poll: settlement can lag a few seconds after close order.
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
                # Closed positions expose PNL; still-open may only have unrealizedPNL.
                if st in ("closed", "liquidated", "expired") or pos.get("PNL") is not None:
                    break

            if not pos:
                # Fallback: scan past positions list for this id
                try:
                    past = await trading_client.list_positions(status="past", page_size=50)
                    pos = next((p for p in past if int(p.get("id", -1)) == pid), None)
                except Exception as e:
                    log.debug("past positions lookup failed: %s", e)

            if not pos:
                details.append({"symbol": leg.get("symbol"), "position_id": pid, "error": "position not found"})
                continue

            pnl = _parse_money(pos.get("PNL"))
            if pnl is None:
                # Last resort: mark-to-market if still reporting unrealized after close attempt
                pnl = _parse_money(pos.get("unrealizedPNL"))

            entry = {
                "symbol": leg.get("symbol"),
                "position_id": pid,
                "status": pos.get("status"),
                "side": pos.get("side"),
                "entryPrice": pos.get("entryPrice"),
                "exitPrice": pos.get("exitPrice"),
                "PNL": pos.get("PNL"),
                "PNLPercent": pos.get("PNLPercent"),
                "parsed_pnl": pnl,
            }
            if pnl is not None:
                total += pnl
                found += 1
            details.append(entry)

            # Annotate matching close leg
            if pid in close_by_pid:
                close_by_pid[pid]["exchange_pnl"] = pnl
                close_by_pid[pid]["exchange_position"] = {
                    k: pos.get(k)
                    for k in ("status", "PNL", "PNLPercent", "entryPrice", "exitPrice", "closedAt")
                }

        if found == 0:
            return None, details
        return float(total), details

    async def _check_exit(self, db, group, open_trade: Trade, latest_prices, backbone, trading_client, trading_mode):
        fit = open_trade.ols_fit
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
        legs_close = await self._place_legs(trading_client, legs_close, is_close=True)

        qty_y = float(legs_entry[0]["qty"]) if legs_entry else notional / max(
            float((open_trade.entry_prices or latest_prices).get(group.dependent_symbol, 1)), 1e-12
        )
        gross = legs_gross_notional(legs_entry)
        model_pnl = residual_cash_pnl(
            open_trade.entry_residual, resid_now, open_trade.direction,
            qty_y, cost_rate, gross,
        )
        fee_model = cost_rate * 2.0 * gross

        # --- Account PnL for live; model for paper ---
        realized = None
        exchange_details = None
        if trading_mode == "live" and isinstance(trading_client, NobitexClient):
            try:
                realized, exchange_details = await self._realized_pnl_from_exchange(
                    trading_client, legs_entry, legs_close,
                )
            except Exception:
                log.exception("failed to read exchange PnL for trade #%s", open_trade.id)
                realized, exchange_details = None, None

        if realized is not None:
            reported_pnl = float(realized)
            open_trade.realized_pnl = float(realized)
            open_trade.pnl = float(realized)  # UI / metrics use this
            log.info(
                "group %s trade #%s LIVE account PnL=%.6g (model was %.6g) details=%s",
                group.id, open_trade.id, realized, model_pnl, exchange_details,
            )
        else:
            reported_pnl = float(model_pnl)
            open_trade.pnl = float(model_pnl)
            open_trade.realized_pnl = None
            if trading_mode == "live":
                log.warning(
                    "group %s trade #%s live close: no exchange PnL yet — storing model_pnl=%.6g",
                    group.id, open_trade.id, model_pnl,
                )

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
        open_trade.realized_fee = None
        if open_trade.trade_notional is None:
            open_trade.trade_notional = notional
        db.commit()

        log.info(
            "group %s trade #%s closed mode=%s reason=%s pnl=%.6g model_pnl=%.6g",
            group.id, open_trade.id, trading_mode, exit_dec.reason, reported_pnl, model_pnl,
        )

    async def _check_entry(
        self, db, group, price_df, latest_prices, backbone, trading_client, trading_mode, is_running: bool = True,
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

        if not is_running:
            return
        if not stat.passed or fit_res.resid_std == 0:
            return

        resid_now = float(fit_res.residual[-1])
        entry_dec = decide_entry(resid_now, fit_res.resid_mean, fit_res.resid_std, backbone["z_entry"])
        if not entry_dec.should_enter:
            return

        notional = float(backbone.get("trade_notional", 100) or 100)
        dep = group.dependent_symbol
        if str(dep).upper().endswith(("IRT", "RLS")):
            notional = max(notional, MIN_ORDER_VALUE_IRT)

        legs = leg_orders(
            group.dependent_symbol,
            entry_dec.direction,
            fit_res.betas,
            latest_prices,
            notional,
        )

        try:
            legs = await self._place_legs(trading_client, legs, is_close=False)
        except Exception as e:
            self._last_error = str(e)
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
            [(l["symbol"], l["side"], round(l["qty"], 8), l.get("position_id")) for l in legs],
        )


bot_engine = BotEngine()
