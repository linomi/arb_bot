"""
The actual running bot: an asyncio background task, started once when the
web server starts, that ticks forever at `backbone.sampling_time` seconds.

Per tick, for every group with status == "active":
  1. if it has an open trade -> check close/stop-loss (frozen OLS params)
  2. else -> refit OLS on the window, run ADF/KPSS, and on a pass, check
     for an entry signal

Legs are beta-aware (see backend/strategy/sizing.py). Each closed trade stores:
  - model_pnl: residual-based theoretical fraction (simulate_pnl)
  - realized_pnl / realized_fee: cash PnL from actual leg fills
  - legs_entry / legs_close: executed basket
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
from backend.utils import seconds_to_resolution, fetch_price_df
from backend.strategy.ols import fit_ols, residual_from_frozen_fit
from backend.strategy.stats_tests import test_stationarity
from backend.strategy.zscore import decide_entry, decide_exit
from backend.strategy.sizing import leg_orders, close_legs_from_entry
from backend.strategy.pnl import realized_cash_pnl
from backend.engine.backtester import simulate_pnl

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
        trading_client = factory.build_trading_client(db)
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
        open_trade = db.query(Trade).filter_by(group_id=group.id, status="open").first()

        if open_trade is not None:
            if is_running:
                await self._check_exit(
                    db, group, open_trade, latest_prices, backbone_local, trading_client, trading_mode,
                )
        else:
            await self._check_entry(
                db, group, price_df, latest_prices, backbone_local, trading_client, trading_mode, is_running,
            )

    async def _place_legs(self, trading_client, legs: list[dict]):
        for leg in legs:
            await trading_client.place_order(
                leg["symbol"], leg["side"], leg["qty"], price=None,
            )

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

        num_legs = len(group.symbols)
        cost_rate = float(backbone["fee_rate"]) + float(backbone["slippage_rate"])
        fee_rate = float(backbone["fee_rate"])
        notional = float(
            open_trade.trade_notional
            if open_trade.trade_notional is not None
            else (backbone.get("trade_notional", 100) or 100)
        )

        model_frac = simulate_pnl(
            open_trade.entry_residual, resid_now, open_trade.direction, num_legs, cost_rate,
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

        await self._place_legs(trading_client, legs_close)

        real_pnl, real_fee = realized_cash_pnl(legs_entry, legs_close, fee_rate)

        open_trade.status = "closed"
        open_trade.close_time = dt.datetime.utcnow()
        open_trade.close_reason = exit_dec.reason
        open_trade.close_z = exit_dec.z
        open_trade.close_residual = resid_now
        open_trade.close_prices = latest_prices
        open_trade.legs_entry = legs_entry
        open_trade.legs_close = legs_close
        open_trade.pnl = float(model_frac)
        open_trade.model_pnl = float(model_frac)
        open_trade.fee_paid = float(cost_rate * num_legs)
        open_trade.realized_pnl = float(real_pnl)
        open_trade.realized_fee = float(real_fee)
        if open_trade.trade_notional is None:
            open_trade.trade_notional = notional
        db.commit()

        log.info(
            "group %s trade #%s closed reason=%s model_pnl=%.6g realized_pnl=%.6g",
            group.id, open_trade.id, exit_dec.reason, model_frac, real_pnl,
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
        legs = leg_orders(
            group.dependent_symbol,
            entry_dec.direction,
            fit_res.betas,
            latest_prices,
            notional,
        )
        await self._place_legs(trading_client, legs)

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
            "group %s opened %s trade notional=%.4g legs=%s",
            group.id, entry_dec.direction, notional,
            [(l["symbol"], l["side"], round(l["qty"], 8)) for l in legs],
        )


bot_engine = BotEngine()
