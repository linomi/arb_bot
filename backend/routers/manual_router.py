"""Manual group creation endpoint (user-selected symbols)."""
import asyncio
import logging

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend import config_service
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
        got = await asyncio.gather(*[md.get_ohlc(sym, resolution, bars) for sym in symbols], return_exceptions=True)
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
        res = await asyncio.get_running_loop().run_in_executor(
            None, lambda: evaluate_group(df, symbols, dependent, params, source="manual"),
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

    metrics, bt_error = await _backtest_manual(db, uniq, dependent, exchange)

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
    out = _group_to_dict(g)
    out["backtest_ok"] = metrics is not None
    out["backtest_error"] = bt_error
    return out
