"""
The actual running bot: an asyncio background task, started once when the
web server starts, that ticks forever at `backbone.sampling_time` seconds.

Whether it *does* anything on a given tick is controlled by BotState.is_running
(toggled by the UI's start/stop button). The loop itself never stops just
because a browser tab closes -- per spec, the UI is a viewer/controller, not
the process the bot runs inside.

Per tick, for every group with status == "active":
  1. if it has an open trade -> check close/stop-loss (frozen OLS params)
  2. else -> refit OLS on the window, run ADF/KPSS, and on a pass, check
     for an entry signal

PnL is stored as a *fraction of trade_notional* (unit-notional return).
Absolute currency PnL is always: stored_fraction * current trade_notional,
so changing trade_notional in the UI rescales all historical metrics.
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
                # Always run the cycle for active groups so OLS fits (and thus
                # the residual chart) stay fresh even when the bot is paused.
                # Order placement is gated inside _process_group by is_running.
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
                    await self._process_group(db, group, backbone, md_client, trading_client, resolution, state.trading_mode, state.is_running)
                except Exception:
                    self._last_error = traceback.format_exc()
                    log.exception("group %s failed this cycle", group.id)
        finally:
            await md_client.aclose()
            if not isinstance(trading_client, PaperExchangeClient):
                await trading_client.aclose()

    async def _process_group(self, db, group: Group, backbone, md_client, trading_client, resolution, trading_mode, is_running: bool):
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
                await self._check_exit(db, group, open_trade, latest_prices, backbone_local, trading_client, trading_mode)
        else:
            await self._check_entry(db, group, price_df, latest_prices, backbone_local, trading_client, trading_mode, is_running)

    async def _check_exit(self, db, group, open_trade: Trade, latest_prices, backbone, trading_client, trading_mode):
        fit = open_trade.ols_fit
        resid_now = residual_from_frozen_fit(group.dependent_symbol, latest_prices, fit.betas, fit.intercept)
        exit_dec = decide_exit(
            resid_now, open_trade.direction, fit.resid_mean, fit.resid_std,
            backbone["z_close"], backbone["z_stop_loss"],
        )
        if not exit_dec.should_exit:
            return

        num_legs = len(group.symbols)
        cost_rate = backbone["fee_rate"] + backbone["slippage_rate"]
        notional = float(backbone.get("trade_notional", 100) or 100)
        # Unit-notional (fractional) PnL — scale by current trade_notional on read
        pnl_fraction = simulate_pnl(
            open_trade.entry_residual, resid_now, open_trade.direction, num_legs, cost_rate
        )

        # Execute closing legs (paper: simulated fill; live: real market orders).
        for sym in group.symbols:
            side = self._closing_side(group, sym, open_trade.direction)
            amount = notional / max(latest_prices[sym], 1e-12)
            await trading_client.place_order(sym, side, amount, price=None)

        open_trade.status = "closed"
        open_trade.close_time = dt.datetime.utcnow()
        open_trade.close_reason = exit_dec.reason
        open_trade.close_z = exit_dec.z
        open_trade.close_residual = resid_now
        open_trade.close_prices = latest_prices
        open_trade.pnl = float(pnl_fraction)  # fraction of notional
        open_trade.fee_paid = float(cost_rate * num_legs)  # fraction of notional
        db.commit()

    async def _check_entry(self, db, group, price_df, latest_prices, backbone, trading_client, trading_mode, is_running: bool = True):
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
        for sym in group.symbols:
            side = self._opening_side(group, sym, entry_dec.direction)
            amount = notional / max(latest_prices[sym], 1e-12)
            await trading_client.place_order(sym, side, amount, price=None)

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
        )
        db.add(trade)
        db.commit()

    @staticmethod
    def _opening_side(group: Group, symbol: str, direction: str) -> str:
        is_dependent = symbol == group.dependent_symbol
        if direction == "short_residual":
            return "sell" if is_dependent else "buy"
        return "buy" if is_dependent else "sell"

    @staticmethod
    def _closing_side(group: Group, symbol: str, open_direction: str) -> str:
        opening = BotEngine._opening_side(group, symbol, open_direction)
        return "sell" if opening == "buy" else "buy"


bot_engine = BotEngine()
