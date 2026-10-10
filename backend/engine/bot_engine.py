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
from backend.strategy.half_life import half_life_ok
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
from backend.strategy.account import paper_free_balance, leverage_for, trade_gross
from backend.notify import emit
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
        # group_id -> latest entry-evaluation result ("why is there no trade?")
        self._group_diag: dict[int, dict] = {}
        self._entry_cooldown_until: dict[int, float] = {}   # group_id -> monotonic deadline
        self._last_fit_logged: dict[int, float] = {}        # group_id -> monotonic time
        self._orphan_positions: list[dict] = []             # exchange positions without open trade
        self._last_orphan_check: float = 0.0
        self._lock = asyncio.Lock()                         # one cycle / close-all at a time
        self._known_orphans: set[str] = set()

    def start_background_loop(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop_forever())

    async def _loop_forever(self):
        while True:
            db = SessionLocal()
            try:
                state = get_or_create_bot_state(db)
                async with self._lock:
                    await self._run_cycle(db, state)
                sampling_time = config_service.get_section(db, "backbone").get("sampling_time", 60)
                self._write_heartbeat()
            except Exception:
                self._last_error = traceback.format_exc()
                log.exception("bot cycle failed")
                emit("cycle_error", error=self._last_error.strip().splitlines()[-1])
                sampling_time = 30
            finally:
                db.close()
            await asyncio.sleep(max(1, int(sampling_time)))

    def _write_heartbeat(self) -> None:
        """Touch data/heartbeat so an external watchdog can tell the loop is alive."""
        try:
            from backend.db import DATA_DIR
            (DATA_DIR / "heartbeat").write_text(str(int(time.time())))
        except Exception:
            pass

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

        # Orphan position reconcile on start and every ~N cycles (default 10 * sampling)
        try:
            interval = float(backbone.get("orphan_reconcile_interval_sec", 600) or 600)
            if time.monotonic() - self._last_orphan_check >= interval:
                await self.reconcile_orphans(db, trading_client, trading_mode, exchange)
        except Exception as e:
            log.warning("orphan reconcile error: %s", e)

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
                    emit("cycle_error", group=getattr(group, "name", gid), error=err.strip().splitlines()[-1])
            self._log_scan_summary(groups)
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
            self._diag(group, "config_error", str(e))
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
            self._diag(group, "no_data", f"only {0 if price_df.empty else len(price_df)} bars, need {min_bars}")
            return

        effective_window = min(configured_window, len(price_df) - 1)
        if effective_window < min_bars:
            log.warning("group %s: effective window %d too small", group.id, effective_window)
            self._diag(group, "no_data", f"window {effective_window} < {min_bars}")
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
            self._diag(group, "position_open", f"trade #{open_trade.id} open since {open_trade.entry_time}")
            await self._check_exit(
                db, group, open_trade, latest_prices, backbone_local, trading_client, trading_mode,
                exchange=group_ex,
            )
        elif not is_running:
            self._diag(group, "bot_stopped", "bot is stopped: no new entries")
        elif not allow_new_entries:
            self._diag(group, "paused", f"entries paused until {self._pause_until}")
        elif is_running and allow_new_entries:
            if self._data_is_stale(price_df, backbone_local):
                msg = "data stale (latest bar older than staleness threshold)"
                self._diag(group, "data_stale", msg)
                self._group_errors[group.id] = msg
                log.warning("group %s: %s", group.id, msg)
                return
            cap_msg = self._portfolio_caps_exceeded(db, backbone_local, trading_mode)
            if cap_msg:
                self._diag(group, "portfolio_cap", cap_msg)
                self._group_errors[group.id] = cap_msg
                log.info("group %s skip entry: %s", group.id, cap_msg)
                return
            blocked = self.symbols_blocked_by_orphans() & set(group.symbols or [])
            if blocked:
                msg = f"orphan positions block symbols {sorted(blocked)}"
                self._diag(group, "orphan_block", msg)
                self._group_errors[group.id] = msg
                log.error("group %s: %s", group.id, msg)
                return
            await self._check_entry(
                db, group, price_df, latest_prices, backbone_local, trading_client, trading_mode,
                exchange=group_ex,
            )


    async def close_all(self, reason: str = "manual_closeall") -> dict:
        """Stop new entries and close every open trade of the current mode right now.

        Runs under the same lock as the trading cycle, so it cannot race with it. Trades that
        fail to close stay open and are retried by the normal cycle.
        """
        async with self._lock:
            db = SessionLocal()
            md_client = trading_client = None
            try:
                state = get_or_create_bot_state(db)
                state.is_running = False
                db.commit()
                mode = state.trading_mode or "paper"
                backbone = config_service.get_section(db, "backbone")
                trades = db.query(Trade).filter_by(status="open", mode=mode).all()
                result = {"total": len(trades), "closed": 0, "failed": 0}
                if not trades:
                    return result
                md_client = factory.build_market_data_client(db)
                trading_client = factory.build_trading_client(db)
                resolution = seconds_to_resolution(backbone["sampling_time"])
                exchange = (getattr(state, "exchange", None) or "nobitex").strip().lower()
                for t in trades:
                    g = db.get(Group, t.group_id)
                    try:
                        df = await fetch_price_df(md_client, g.symbols, resolution, 40)
                        if df.empty:
                            raise RuntimeError("no prices")
                        prices = df.iloc[-1].to_dict()
                        group_ex = (getattr(g, "exchange", None) or exchange).strip().lower()
                        await self._check_exit(
                            db, g, t, prices, backbone, trading_client, mode,
                            exchange=group_ex, force_reason=reason,
                        )
                        db.refresh(t)
                    except Exception as e:
                        log.exception("close_all: trade #%s failed: %s", t.id, e)
                    if t.status == "closed":
                        result["closed"] += 1
                    else:
                        result["failed"] += 1
                return result
            finally:
                for c in (md_client, trading_client):
                    if c is not None and not isinstance(c, PaperExchangeClient):
                        try:
                            await c.aclose()
                        except Exception:
                            pass
                db.close()

    def _data_is_stale(self, price_df, backbone: dict) -> bool:
        """True if latest bar older than data_staleness_mult * sampling_time."""
        mult = float(backbone.get("data_staleness_mult", 3.0) or 0)
        if mult <= 0 or price_df is None or price_df.empty:
            return False
        st = float(backbone.get("sampling_time", 60) or 60)
        try:
            last = price_df.index[-1]
            import pandas as pd
            if not isinstance(last, pd.Timestamp):
                last = pd.Timestamp(last)
            # assume index is UTC-ish; compare to utcnow
            age = (pd.Timestamp.utcnow().tz_localize(None) - last.tz_localize(None) if last.tzinfo else
                   pd.Timestamp.utcnow().tz_localize(None) - last)
            age_sec = float(age.total_seconds())
            return age_sec > mult * st
        except Exception:
            return False

    def _portfolio_caps_exceeded(self, db, backbone: dict, trading_mode: str) -> str | None:
        max_trades = int(backbone.get("max_open_trades", 0) or 0)
        max_notional = float(backbone.get("max_total_gross_notional", 0) or 0)
        if max_trades <= 0 and max_notional <= 0:
            return None
        opens = db.query(Trade).filter(Trade.status == "open", Trade.mode == trading_mode).all()
        if max_trades > 0 and len(opens) >= max_trades:
            return f"max_open_trades={max_trades} reached ({len(opens)} open)"
        if max_notional > 0:
            total = 0.0
            for tr in opens:
                for leg in (tr.legs_entry or []):
                    q = float(leg.get("filled_qty") or leg.get("qty") or 0)
                    px = float(leg.get("price") or 0)
                    total += abs(q * px)
            if total >= max_notional:
                return f"max_total_gross_notional={max_notional} reached (gross≈{total:.0f})"
        return None


    async def reconcile_orphans(self, db, trading_client, trading_mode: str, exchange: str) -> list[dict]:
        """Compare exchange open positions with DB open trades; record orphans.
        Orphans block new entries for groups that share those symbols.
        """
        orphans: list[dict] = []
        try:
            positions = await trading_client.list_positions()
        except Exception as e:
            log.warning("orphan reconcile list_positions failed: %s", e)
            return self._orphan_positions

        # Collect position ids / symbols covered by open trades (and failed_partial / rollback_failed)
        covered_ids: set[str] = set()
        covered_syms: set[str] = set()
        trades = db.query(Trade).filter(
            Trade.mode == trading_mode,
            Trade.status.in_(("open", "failed_partial")),
        ).all()
        for tr in trades:
            for leg in (tr.legs_entry or []):
                if leg.get("position_id") is not None:
                    covered_ids.add(str(leg["position_id"]))
                if leg.get("symbol"):
                    covered_syms.add(str(leg["symbol"]))
                if leg.get("rollback_failed"):
                    # still count as known, but log
                    log.error("trade %s leg %s has rollback_failed=true", tr.id, leg.get("symbol"))

        for pos in positions or []:
            pid = str(pos.get("id") or "")
            sym = str(pos.get("symbol") or "")
            liab = float(pos.get("liability") or 0)
            if liab <= 0:
                continue
            if pid and pid in covered_ids:
                continue
            if sym and sym in covered_syms:
                continue
            orphan = {
                "id": pid,
                "symbol": sym,
                "side": pos.get("side"),
                "liability": liab,
                "exchange": exchange,
            }
            orphans.append(orphan)
            log.error("ORPHAN POSITION on %s: %s", exchange, orphan)

        self._orphan_positions = orphans
        self._last_orphan_check = time.monotonic()
        keys = {f"{o.get('symbol')}:{o.get('side')}" for o in orphans}
        fresh = [o for o in orphans if f"{o.get('symbol')}:{o.get('side')}" not in self._known_orphans]
        self._known_orphans = keys
        if fresh:
            emit("orphan", orphans=fresh, exchange=exchange)
        if orphans:
            self._last_error = f"{len(orphans)} orphan position(s) on {exchange}"
        return orphans

    def symbols_blocked_by_orphans(self) -> set[str]:
        return {str(o.get("symbol")) for o in self._orphan_positions if o.get("symbol")}


    async def _risk_exit_if_near_liquidation(
        self, open_trade, trading_client, backbone: dict,
    ) -> bool:
        """If any leg mark is within 20% of liquidation, return True to force close."""
        threshold = float(backbone.get("liquidation_proximity_fraction", 0.20) or 0)
        if threshold <= 0 or not open_trade:
            return False
        for leg in (open_trade.legs_entry or []):
            pid = leg.get("position_id")
            if not pid:
                continue
            try:
                pos = await trading_client.get_position(_pos_id(pid))
            except Exception:
                continue
            if not pos:
                continue
            mark = _parse_money(pos.get("markPrice"))
            # common field names for liquidation
            liq = None
            for k in ("liquidationPrice", "liqPrice", "liquidation_price", "bustPrice"):
                liq = _parse_money(pos.get(k))
                if liq is not None and liq > 0:
                    break
            # margin ratio alternative
            ratio = pos.get("marginRatio") or pos.get("margin_ratio") or (pos.get("_raw") or {}).get("marginRatio")
            try:
                ratio = float(ratio) if ratio is not None else None
            except (TypeError, ValueError):
                ratio = None
            near = False
            if mark is not None and liq is not None and liq > 0 and mark > 0:
                # distance as fraction of mark
                dist = abs(mark - liq) / mark
                if dist <= threshold:
                    near = True
            if ratio is not None and ratio >= (1.0 - threshold):
                near = True
            if near:
                log.error(
                    "RISK EXIT trade #%s leg %s mark=%s liq=%s ratio=%s",
                    open_trade.id, leg.get("symbol"), mark, liq, ratio,
                )
                return True
        return False

    def _diag(self, group, stage: str, text: str = "", **fields) -> None:
        """Record the outcome of this cycle's entry evaluation for a group."""
        self._group_diag[group.id] = {
            "group_id": group.id,
            "name": group.name,
            "stage": stage,
            "text": text,
            "at": dt.datetime.utcnow().isoformat() + "Z",
            **fields,
        }
        log.debug("entry eval group %s (%s): %s %s", group.id, group.name, stage, text)

    def _log_scan_summary(self, groups) -> None:
        """One INFO line per cycle: how many groups sit at each stage + the nearest miss."""
        rows = [self._group_diag[g.id] for g in groups if g.id in self._group_diag]
        if not rows:
            return
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["stage"]] = counts.get(r["stage"], 0) + 1
        near = [r for r in rows if r.get("z") is not None and r.get("z_entry")]
        near.sort(key=lambda r: abs(r["z"]) / max(1e-9, r["z_entry"]), reverse=True)
        tail = ""
        if near:
            n = near[0]
            tail = (
                f" | nearest: {n['name']} |z|={abs(n['z']):.2f}/{n['z_entry']:.2f}"
                f" stationary={'yes' if n.get('stationary') else 'no'}"
                + (f" (p={n['adf_p']:.3f})" if n.get("adf_p") is not None else "")
            )
        log.info(
            "entry scan: %d group(s) %s%s",
            len(rows), ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), tail,
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
        paper_free: float | None = None,
        paper_leverage: float = 1.0,
    ) -> tuple[list[dict] | None, float, str]:
        """
        Build legs for desired_notional (the TOTAL gross of all legs). On live, scale down
        so total collateral fits free balance × safety − buffer, while every leg stays ≥
        exchange min. In paper mode the same fit runs against the simulated wallet
        (`paper_free`, margin = gross / `paper_leverage`) when one is configured.

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

        is_live_client = trading_mode == "live" and (
            isinstance(trading_client, NobitexClient)
            or (XTClient is not None and isinstance(trading_client, XTClient))
        )
        if not is_live_client and paper_free is None:
            legs = _build(desired_notional)
            return legs, float(desired_notional), ""

        if is_live_client:
            # Live keeps counting gross at 1x (conservative: ignores leverage).
            lev = 1.0
            free = await self._read_free_balance_irt(trading_client, exchange=exchange)
            if free is None:
                return None, 0.0, (
                    "cannot read margin wallet balance — refusing live entry "
                    "(API key needs wallet read; endpoint /users/wallets/list)"
                )
        else:
            lev = max(1.0, float(paper_leverage or 1.0))
            free = float(paper_free)

        unit = "IRT" if exchange == "nobitex" else "USDT"
        dp = 0 if exchange == "nobitex" else 2
        usable = max(0.0, float(free) * BALANCE_SAFETY_FRACTION - buffer)
        if usable < min_val * 2:
            return None, 0.0, (
                f"free balance too low: free={free:.{dp}f} {unit}, usable≈{usable:.{dp}f} "
                f"(need ≥ {min_val * 2:.{dp}f} for a 2-leg basket)"
            )

        try:
            legs = _build(desired_notional)
        except Exception as e:
            return None, 0.0, f"sizing failed: {e}"
        need = total_required_collateral(legs, leverage=lev)
        notional = float(desired_notional)

        if need > usable:
            if need <= 0:
                return None, 0.0, "zero required collateral?"
            scale = (usable / need) * 0.98
            notional = float(desired_notional) * scale
            if notional < min_val:
                return None, 0.0, (
                    f"insufficient margin: desired need≈{need:.{dp}f} {unit} for "
                    f"notional={desired_notional:.{dp}f}, free={free:.{dp}f}, usable={usable:.{dp}f}"
                )
            try:
                legs = _build(notional)
            except Exception as e:
                return None, 0.0, f"sizing failed after scale: {e}"
            need = total_required_collateral(legs, leverage=lev)
            if need > usable:
                scale2 = (usable / need) * 0.98
                notional = notional * scale2
                if notional < min_val:
                    return None, 0.0, (
                        f"insufficient margin after min-order scale-up: need≈{need:.{dp}f}, "
                        f"usable={usable:.{dp}f}, free={free:.{dp}f}"
                    )
                legs = _build(notional)
                need = total_required_collateral(legs, leverage=lev)
                if need > usable:
                    return None, 0.0, (
                        f"insufficient margin: need≈{need:.{dp}f} {unit}, free={free:.{dp}f}, "
                        f"usable={usable:.{dp}f}"
                    )

        msg = ""
        if notional + 1 < float(desired_notional):
            msg = (
                f"scaled notional {desired_notional:.{dp}f} → {notional:.{dp}f} {unit} "
                f"(collateral need={need:.{dp}f}, free={free:.{dp}f}, usable={usable:.{dp}f})"
            )
            log.warning("%s", msg)
        else:
            log.info(
                "pretrade OK notional=%.0f need=%.0f free=%.0f usable=%.0f",
                notional, need, free, usable,
            )
        return legs, notional, msg


    async def _preflight_legs(self, trading_client, legs: list[dict], exchange: str) -> list[dict]:
        """Validate min notional / precision for every leg BEFORE the first order.
        Returns legs ordered so the highest rejection risk is placed first.
        Raises PartialLegsError-compatible ValueError on hard fails.
        """
        from backend.strategy.sizing import MIN_ORDER_VALUE_IRT
        risks: list[tuple[float, dict]] = []
        for leg in legs:
            symbol = leg["symbol"]
            qty = float(leg.get("qty") or 0)
            px = float(leg.get("price") or 0)
            notional = qty * px if px > 0 else 0.0
            min_val = float(MIN_ORDER_VALUE_IRT)
            if exchange == "nobitex" and hasattr(trading_client, "min_order_value_irt"):
                try:
                    await trading_client.get_margin_markets(details=True)
                    min_val = float(trading_client.min_order_value_irt(symbol))
                except Exception as e:
                    log.debug("preflight min lookup failed for %s: %s", symbol, e)
            elif exchange == "xt" and hasattr(trading_client, "get_min_notional"):
                try:
                    mn = await trading_client.get_min_notional(symbol)
                    if mn is not None:
                        min_val = float(mn)
                except Exception as e:
                    log.debug("preflight XT min failed for %s: %s", symbol, e)
            if notional > 0 and notional < min_val:
                raise ValueError(
                    f"preflight: leg {symbol} notional {notional:.4g} < min {min_val:.4g}"
                )
            if qty <= 0:
                raise ValueError(f"preflight: leg {symbol} qty <= 0")
            # Risk score: closer to min = higher risk of rejection
            ratio = (notional / min_val) if min_val > 0 else 999.0
            risks.append((ratio, leg))
        risks.sort(key=lambda x: x[0])  # smallest ratio first
        return [leg for _, leg in risks]

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
                if is_close and XTClient is not None and isinstance(trading_client, XTClient):
                    # XT is positionSide-based: an un-flagged opposite order
                    # would OPEN a new position instead of closing.
                    kwargs["reduce_only"] = True
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
                        if XTClient is not None and isinstance(trading_client, XTClient):
                            kwargs["reduce_only"] = True
                    await trading_client.place_order(leg["symbol"], opp, qty, **kwargs)
                legs[idx] = {**leg, "rolled_back": True}
                log.info("rolled back leg %s", leg.get("symbol"))
            except Exception as e:
                log.exception("FAILED to roll back leg %s: %s", leg.get("symbol"), e)
                legs[idx] = {**leg, "rollback_failed": True, "rollback_error": str(e)}


    @staticmethod
    def _pnl_from_leg_fills(legs_entry: list[dict], legs_close: list[dict]) -> float | None:
        """Approximate realized cash PnL from entry/exit fill prices and qty."""
        if not legs_entry:
            return None
        close_by_sym = {l.get("symbol"): l for l in (legs_close or [])}
        total = 0.0
        any_fill = False
        for leg in legs_entry:
            sym = leg.get("symbol")
            qty = float(leg.get("filled_qty") or leg.get("qty") or 0)
            entry_px = float(leg.get("fill_price") or leg.get("price") or 0)
            cl = close_by_sym.get(sym) or {}
            exit_px = float(cl.get("fill_price") or cl.get("price") or 0)
            if qty <= 0 or entry_px <= 0 or exit_px <= 0:
                continue
            any_fill = True
            side = str(leg.get("side") or "").lower()
            if side in ("buy", "long"):
                total += (exit_px - entry_px) * qty
            else:
                total += (entry_px - exit_px) * qty
        return float(total) if any_fill else None

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

    async def _check_exit(self, db, group, open_trade: Trade, latest_prices, backbone, trading_client, trading_mode, exchange: str = "nobitex",
                          force_reason: str | None = None):
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
        if force_reason:
            exit_dec = ExitDecision(True, force_reason, exit_dec.z)
        elif (not exit_dec.should_exit and trading_mode == "live"
              and await self._risk_exit_if_near_liquidation(open_trade, trading_client, backbone)):
            exit_dec = ExitDecision(True, "risk_exit", exit_dec.z)
            emit("risk_exit", group=group.name, trade_id=open_trade.id)
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
            # Rows saved before the gross-notional change (notional_basis NULL) stored the
            # dependent leg's notional; a missing trade_notional falls back to the current
            # config value, which is gross.
            basis = "gross" if open_trade.trade_notional is None else (open_trade.notional_basis or "dependent")
            legs_entry = leg_orders(
                group.dependent_symbol,
                open_trade.direction,
                fit.betas or {},
                open_trade.entry_prices or latest_prices,
                notional,
                basis=basis,
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
            emit("exit_failed", group=group.name, trade_id=open_trade.id, error=str(e)[:300])
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
            emit("exit_failed", group=group.name, trade_id=open_trade.id,
                 error="legs not closed: " + ", ".join(str(l.get("symbol")) for l in still_open))
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

        if realized is None:
            # Fallback: reconstruct from entry/exit fills (XT often lacks position PNL)
            realized = self._pnl_from_leg_fills(legs_entry, legs_close)
            if realized is not None:
                log.info("group %s trade #%s PnL from fills=%.6g", group.id, open_trade.id, realized)

        if realized is not None:
            reported_pnl = float(realized)
            open_trade.realized_pnl = float(realized)
            open_trade.pnl = float(realized)
            open_trade.pnl_source = "exchange"
            log.info(
                "group %s trade #%s LIVE PnL=%.6g (model %.6g)",
                group.id, open_trade.id, realized, model_pnl,
            )
            # Flag large divergence between exchange and model
            if model_pnl is not None and abs(model_pnl) > 1e-12:
                rel = abs(float(realized) - float(model_pnl)) / max(abs(float(model_pnl)), 1e-12)
                if rel > 0.20:
                    open_trade.pnl_divergence = True
                    log.warning(
                        "group %s trade #%s PnL divergence >20%%: exchange=%.6g model=%.6g (rel=%.1f%%)",
                        group.id, open_trade.id, realized, model_pnl, 100 * rel,
                    )
        else:
            reported_pnl = float(model_pnl)
            open_trade.pnl = float(model_pnl)
            open_trade.realized_pnl = None
            open_trade.pnl_source = "model"

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
            open_trade.notional_basis = "gross"
        db.commit()

        log.info(
            "group %s trade #%s closed mode=%s reason=%s pnl=%.6g",
            group.id, open_trade.id, trading_mode, exit_dec.reason, reported_pnl,
        )
        emit(
            "closed", group=group.name, trade_id=open_trade.id, mode=trading_mode,
            reason=exit_dec.reason, z=exit_dec.z, pnl=reported_pnl,
            pnl_source=open_trade.pnl_source,
            held_sec=((open_trade.close_time - open_trade.entry_time).total_seconds()
                      if open_trade.entry_time and open_trade.close_time else None),
        )

    async def _check_entry(
        self, db, group, price_df, latest_prices, backbone, trading_client, trading_mode,
        exchange: str = "nobitex",
    ):
        window = price_df.iloc[-int(backbone["window_size"]):]
        price_matrix = {s: window[s].to_numpy() for s in group.symbols}
        try:
            fit_res = fit_ols(group.dependent_symbol, price_matrix)
        except Exception as e:
            self._diag(group, "fit_failed", f"OLS failed: {e}")
            return

        if self._in_cooldown(group.id):
            left = max(0.0, self._entry_cooldown_until.get(group.id, 0.0) - time.monotonic())
            self._diag(group, "cooldown", f"retry cooldown after a failed entry, {left:.0f}s left")
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
        hl_frac = float(backbone.get("half_life_max_fraction", 1.0 / 3.0) or 0)
        hl_ok = True
        if hl_frac > 0 and fit_res.resid_std != 0:
            hl_ok, _ = half_life_ok(
                fit_res.residual, int(backbone["window_size"]), max_fraction=hl_frac,
            )
        candidate = bool(stat.passed and fit_res.resid_std != 0 and entry_dec.should_enter and hl_ok)

        # --- diagnostics: every reason that currently blocks an entry ---------
        z_entry_cfg = float(backbone["z_entry"])
        blockers: list[str] = []
        if fit_res.resid_std == 0:
            blockers.append("residual std is 0")
        if entry_dec.reason == "beyond_stop":
            blockers.append(f"|z|={abs(entry_dec.z):.2f} is beyond the stop level")
        elif not entry_dec.should_enter:
            blockers.append(f"|z|={abs(entry_dec.z):.2f} < entry {z_entry_cfg:g}")
        if not stat.passed:
            bits = []
            if backbone.get("stationarity_method", "engle_granger") == "engle_granger":
                bits.append(f"cointegration p={stat.adf_pvalue:.3f} (need <= {backbone['adf_alpha']})")
            else:
                bits.append(f"ADF p={stat.adf_pvalue:.3f} (need <= {backbone['adf_alpha']})")
            if not (stat.kpss_pvalue >= backbone["kpss_alpha"]):
                bits.append(f"KPSS p={stat.kpss_pvalue:.3f} (need >= {backbone['kpss_alpha']})")
            blockers.append("not stationary: " + ", ".join(bits))
        if not hl_ok:
            blockers.append("mean-reversion half-life outside the allowed range")
        diag_fields = dict(
            z=float(entry_dec.z), z_entry=z_entry_cfg, stationary=bool(stat.passed),
            adf_p=float(stat.adf_pvalue), kpss_p=float(stat.kpss_pvalue),
            half_life_ok=bool(hl_ok), n_symbols=len(group.symbols or []),
            blockers=blockers,
        )
        if blockers:
            order = ("not stationary", "mean-reversion", "|z|", "residual")
            first = next((b for b in blockers if b.startswith(order[0])), None) or blockers[0]
            stage = (
                "not_stationary" if first.startswith("not stationary")
                else "half_life" if first.startswith("mean-reversion")
                else "beyond_stop" if "beyond" in first
                else "waiting_z" if first.startswith("|z|")
                else "invalid_fit"
            )
            self._diag(group, stage, "; ".join(blockers), **diag_fields)
        else:
            self._diag(group, "signal", f"entry signal z={entry_dec.z:.2f}, checking gates", **diag_fields)

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
                self._diag(group, "symbol_clash", f"symbols {sorted(clash)} used by another open trade", **diag_fields)
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
                self._diag(
                    group, "profit_gate",
                    f"net edge too small: {gate_det.get('reject_reason')} "
                    f"(sigma_rel={gate_det.get('sigma_rel')}, needs z>={gate_det.get('z_min')})",
                    **diag_fields,
                )
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
            self._diag(group, "gate_error", str(e), **diag_fields)
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
            self._diag(group, "scale_cap", str(e), **diag_fields)
            return

        # Paper mode: the same balance fit as live, against the simulated shared USDT wallet.
        paper_free = paper_lev = None
        if trading_mode != "live" and group_ex == "xt":
            paper_free = paper_free_balance(db, backbone)
            paper_lev = leverage_for(group_ex, backbone, group.dependent_symbol)
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
            paper_free=paper_free,
            paper_leverage=paper_lev,
        )
        if legs is None:
            self._last_error = bal_msg
            self._group_errors[group.id] = bal_msg
            self._diag(group, "sizing_blocked", bal_msg, **diag_fields)
            log.error("group %s entry blocked: %s", group.id, bal_msg)
            self._start_cooldown(group.id, backbone)
            return
        if bal_msg:
            log.info("group %s: %s", group.id, bal_msg)

        try:
            group_ex = (getattr(group, "exchange", None) or exchange or "nobitex").lower()
            legs = await self._preflight_legs(trading_client, legs, group_ex)
            legs = await self._place_legs(
                trading_client, legs, is_close=False,
                attempt_id=f"e{group.id}",
            )
        except ValueError as e:
            self._last_error = str(e)
            self._group_errors[group.id] = str(e)
            log.error("group %s entry preflight failed: %s", group.id, e)
            self._start_cooldown(group.id, backbone)
            return
        except PartialLegsError as e:
            self._last_error = str(e)
            self._group_errors[group.id] = str(e)
            log.error("group %s partial entry (rolled back if possible): %s", group.id, e)
            self._diag(group, "order_failed", f"partial entry rolled back: {e}", **diag_fields)
            bad = [l for l in (e.legs or []) if l.get("rollback_failed")]
            if bad:
                emit("rollback_failed", group=group.name, symbols=[l.get("symbol") for l in bad])
            else:
                emit("entry_failed", group=group.name, error=str(e)[:300], rolled_back=True)
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
                notional_basis="gross",
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
            self._diag(group, "order_failed", str(e), **diag_fields)
            emit("entry_failed", group=group.name, error=str(e)[:300], rolled_back=False)
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
            notional_basis="gross",
        )
        db.add(trade)
        db.commit()
        self._diag(group, "entered", f"opened {entry_dec.direction} at z={entry_dec.z:.2f}", **diag_fields)
        _g = legs_gross_notional(legs)
        emit(
            "entered", group=group.name, trade_id=trade.id, mode=trading_mode,
            direction=entry_dec.direction, z=entry_dec.z, legs=legs, gross=_g,
            margin=_g / leverage_for(group_ex, backbone, group.dependent_symbol),
        )
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