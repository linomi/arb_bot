"""
Background trading loop. Raw-price OLS residual; beta share ratios; cash PnL.

Live path:
  - margin orders with leverage 1
  - respects rate limits (via NobitexClient)
  - enforces min 50k IRT per leg (via sizing)
  - closes via /positions/{id}/close when position_id is known
"""
import asyncio
import datetime as dt
import logging
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
        # Only consider open trades for THIS mode (paper/live separation).
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
        """
        Place or close legs. Mutates each leg with exchange response fields
        (order_id, position_id when resolvable). Small delay between legs to
        respect shared order rate limit.
        """
        out = []
        for i, leg in enumerate(legs):
            if i > 0:
                await asyncio.sleep(0.35)

            symbol = leg["symbol"]
            side = leg["side"]
            qty = float(leg["qty"])
            price = leg.get("price")

            # Prefer official position-close endpoint when we have position_id.
            if is_close and isinstance(trading_client, NobitexClient) and leg.get("position_id"):
                try:
                    resp = await trading_client.close_position(
                        int(leg["position_id"]),
                        amount=qty,
                        execution="market",
                    )
                    leg = {**leg, "close_response": resp, "closed_via": "position_close"}
                    out.append(leg)
                    continue
                except NobitexError as e:
                    log.warning("position close failed id=%s: %s — falling back to opposite order", leg.get("position_id"), e)

            resp = await trading_client.place_order(symbol, side, qty, price=None)
            order = (resp or {}).get("order") or {}
            order_id = order.get("id") or (resp or {}).get("id")
            leg = {**leg, "order_response": resp, "order_id": order_id}

            # After open, try to bind position id (live only).
            if (
                not is_close
                and isinstance(trading_client, NobitexClient)
                and order_id
            ):
                try:
                    await asyncio.sleep(0.5)
                    pid = await trading_client.resolve_position_id(symbol, side)
                    if pid:
                        leg["position_id"] = pid
                except Exception as e:
                    log.debug("resolve_position_id failed: %s", e)

            out.append(leg)
        return out

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
        cash_pnl = residual_cash_pnl(
            open_trade.entry_residual, resid_now, open_trade.direction,
            qty_y, cost_rate, gross,
        )
        fee_paid = cost_rate * 2.0 * gross

        open_trade.status = "closed"
        open_trade.close_time = dt.datetime.utcnow()
        open_trade.close_reason = exit_dec.reason
        open_trade.close_z = exit_dec.z
        open_trade.close_residual = resid_now
        open_trade.close_prices = latest_prices
        open_trade.legs_entry = legs_entry
        open_trade.legs_close = legs_close
        open_trade.pnl = float(cash_pnl)
        open_trade.model_pnl = float(cash_pnl)
        open_trade.fee_paid = float(fee_paid)
        open_trade.realized_pnl = None
        open_trade.realized_fee = None
        if open_trade.trade_notional is None:
            open_trade.trade_notional = notional
        db.commit()

        log.info(
            "group %s trade #%s closed mode=%s reason=%s pnl=%.6g",
            group.id, open_trade.id, trading_mode, exit_dec.reason, cash_pnl,
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
        # Ensure config notional is at least the exchange floor for IRT deps.
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
