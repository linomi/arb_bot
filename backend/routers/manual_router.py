"""Manual group creation endpoint (user-selected symbols)."""
import asyncio
import logging

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend import config_service, init_progress
from backend.exchange import factory
from backend.utils import seconds_to_resolution

from backend.db import get_db
from backend.models import Group
from backend.schemas import ManualGroupCreate
from backend.group_names import generate_group_name
from backend.strategy.sizing import assert_same_quote, assert_same_exchange
from backend.bot_state_service import get_or_create_bot_state

log = logging.getLogger("manual_router")

router = APIRouter(prefix="/api/groups", tags=["groups"])


async def _backtest_manual(db, symbols: list[str], dependent: str, exchange: str):
    """Backtest the chosen group like the automatic init does. Returns (metrics | None, error | None)."""
    from backend.engine.init_methods import evaluate_group, _align_price_df
    from backend.routers.init_router import _backbone_params

    init_cfg = dict(config_service.get_section(db, "init"))
    backbone_cfg = dict(config_service.get_section(db, "backbone"))
    st = max(1, int(backbone_cfg.get("sampling_time", 60) or 60))
    resolution = seconds_to_resolution(st)
    bars = max(int(backbone_cfg["window_size"]) + 10, int(float(init_cfg.get("backtest_days", 3)) * 86400 / st))
    md = None
    try:
        md = factory.build_market_data_client(db)
        n_sym = len(symbols)
        init_progress.update(phase="fetch_ohlc", message=f"Downloading prices for {n_sym} symbols ({bars} bars, {resolution})…",
                             current=0, total=n_sym, percent=5)
        done = 0

        async def _one(sym):
            nonlocal done
            data = await md.get_ohlc(sym, resolution, bars)
            done += 1
            init_progress.update(phase="fetch_ohlc", message=f"Downloaded {done}/{n_sym}: {sym}",
                                 current=done, total=n_sym, percent=5 + 40 * done / n_sym)
            return data

        got = await asyncio.gather(*[_one(sym) for sym in symbols], return_exceptions=True)
        ohlc = {}
        for sym, item in zip(symbols, got):
            if isinstance(item, Exception):
                return None, f"price download failed for {sym}: {item}"
            ohlc[sym] = item
        df = _align_price_df(ohlc)
        missing = [s for s in symbols if s not in df.columns]
        if missing or len(df) < int(backbone_cfg["window_size"]) + 5:
            return None, f"not enough aligned price history ({len(df)} bars) for {', '.join(missing) or 'the group'}"
        params = _backbone_params(backbone_cfg, exchange)
        init_progress.update(phase="backtest", message=f"Backtesting {len(df)} bars…", current=0, total=100, percent=45)

        def _cb(frac: float):
            init_progress.update(phase="backtest", message=f"Backtesting… {frac * 100:.0f}%",
                                 current=int(frac * 100), total=100, percent=45 + 50 * frac)

        res = await asyncio.get_running_loop().run_in_executor(
            None, lambda: evaluate_group(df, symbols, dependent, params, source="manual", progress_cb=_cb),
        )
        if res is None:
            return None, "backtest produced no result"
        return res["backtest_metrics"], None
    except Exception as e:  # never block group creation on a backtest problem
        log.exception("manual group backtest failed")
        return None, str(e) or e.__class__.__name__
    finally:
        if md is not None:
            try:
                await md.aclose()
            except Exception:
                pass


def _group_to_dict(g: Group) -> dict:
    return {
        "id": g.id, "name": g.name, "symbols": g.symbols,
        "dependent_symbol": g.dependent_symbol, "source": g.source,
        "sector": g.sector, "status": g.status,
        "exchange": getattr(g, "exchange", None) or "nobitex",
        "backtest_metrics": g.backtest_metrics,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


@router.post("/manual")
async def create_manual_group(body: ManualGroupCreate, db: Session = Depends(get_db)):
    symbols = [s.strip() for s in (body.symbols or []) if s and str(s).strip()]
    # Preserve unified ccxt form (BTC/USDT:USDT); only upper-case non-slash forms
    normalized = []
    for s in symbols:
        if "/" in s or ":" in s:
            normalized.append(s)  # keep unified form as-is (ccxt is case-sensitive-ish)
        else:
            normalized.append(s.upper())
    seen = set()
    uniq = []
    for s in normalized:
        key = s.upper()
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    if len(uniq) < 2:
        raise HTTPException(400, "Select at least 2 distinct symbols")

    state = get_or_create_bot_state(db)
    exchange = (body.exchange or getattr(state, "exchange", None) or "nobitex").strip().lower()

    dependent = body.dependent_symbol or uniq[0]
    if dependent.upper() not in {u.upper() for u in uniq}:
        # try exact match
        if dependent not in uniq:
            raise HTTPException(
                400,
                f"dependent_symbol {dependent} must be one of the selected symbols",
            )

    try:
        assert_same_exchange(uniq, exchange, dependent)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not init_progress.start("Creating manual group…"):
        raise HTTPException(409, "Initialization is already running. Wait for it to finish.")
    try:
        metrics, bt_error = await _backtest_manual(db, uniq, dependent, exchange)
        init_progress.update(phase="persist", message="Saving group…", percent=96)
        used = {n for (n,) in db.query(Group.name).all()}
        name = (body.name or "").strip() or generate_group_name(used=used)
        g = Group(
            name=name,
            symbols=uniq,
            dependent_symbol=dependent,
            source="manual",
            sector=None,
            status="active" if body.activate else "candidate",
            exchange=exchange,
            params_snapshot=dict(config_service.get_section(db, "backbone")),
            backtest_metrics=metrics,
        )
        db.add(g)
        db.commit()
        db.refresh(g)
    except BaseException as e:
        try:
            db.rollback()
        except Exception:
            pass
        init_progress.fail(str(e) or e.__class__.__name__)
        raise
    out = _group_to_dict(g)
    if metrics is not None:
        init_progress.finish({"method": "manual", "groups_created": [g.id]})
    else:
        init_progress.fail(f"Group created, but the backtest is not available: {bt_error}")
    out["backtest_ok"] = metrics is not None
    out["backtest_error"] = bt_error
    return out
