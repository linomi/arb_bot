import asyncio
import datetime as dt
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.db import get_db
from backend import config_service
from backend.models import Group, OLSFit, Trade
from backend.schemas import GroupStatusUpdate
from backend.strategy.metrics import compute_group_performance
from backend.strategy.ols import fit_ols, residual_from_frozen_fit
from backend.strategy.stats_tests import test_stationarity
from backend.exchange import factory
from backend.exchange.paper import PaperExchangeClient
from backend.exchange.nobitex import NobitexClient
from backend.utils import seconds_to_resolution, fetch_price_df
from backend.group_names import generate_group_name, is_legacy_name
from backend.bot_state_service import get_or_create_bot_state

log = logging.getLogger("groups_router")
router = APIRouter(prefix="/api/groups", tags=["groups"])


def _sanitize_metrics(m):
    if not isinstance(m, dict):
        return m
    out = {}
    for k, v in m.items():
        if isinstance(v, float):
            if v != v:
                out[k] = 0.0
            elif v == float("inf"):
                out[k] = 999.0 if k == "profit_factor" else 0.0
            elif v == float("-inf"):
                out[k] = 0.0
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def _group_to_dict(g: Group) -> dict:
    return {
        "id": g.id, "name": g.name, "symbols": g.symbols,
        "dependent_symbol": g.dependent_symbol, "source": g.source,
        "sector": g.sector, "status": g.status,
        "backtest_metrics": _sanitize_metrics(g.backtest_metrics),
        "created_at": g.created_at.isoformat(),
    }


def _fit_to_dict(f: OLSFit) -> dict:
    return {
        "id": f.id, "group_id": f.group_id, "fitted_at": f.fitted_at.isoformat(),
        "window_start": f.window_start.isoformat() if f.window_start else None,
        "window_end": f.window_end.isoformat() if f.window_end else None,
        "betas": f.betas, "intercept": f.intercept,
        "resid_mean": f.resid_mean, "resid_std": f.resid_std,
        "adf_stat": f.adf_stat, "adf_pvalue": f.adf_pvalue,
        "kpss_stat": f.kpss_stat, "kpss_pvalue": f.kpss_pvalue,
        "passed": f.passed, "residual_series": f.residual_series,
    }


def _trade_pnl(t: Trade):
    """
    Display / metrics PnL:
      live  → ONLY exchange realized_pnl (never model). Null if missing.
      paper → model residual cash pnl
    """
    if getattr(t, "mode", None) == "live":
        if getattr(t, "realized_pnl", None) is not None:
            return float(t.realized_pnl)
        # Do NOT fall back to model for live — that is what inflated the UI loss.
        return None
    if t.pnl is not None:
        return float(t.pnl)
    if getattr(t, "model_pnl", None) is not None:
        return float(t.model_pnl)
    return None


def _trade_to_dict(t: Trade) -> dict:
    pnl = _trade_pnl(t)
    realized = float(t.realized_pnl) if getattr(t, "realized_pnl", None) is not None else None
    model = float(t.model_pnl) if getattr(t, "model_pnl", None) is not None else None
    return {
        "id": t.id, "group_id": t.group_id, "ols_fit_id": t.ols_fit_id,
        "direction": t.direction, "mode": t.mode,
        "entry_time": t.entry_time.isoformat(), "entry_z": t.entry_z,
        "entry_residual": t.entry_residual, "entry_prices": t.entry_prices,
        "status": t.status,
        "close_time": t.close_time.isoformat() if t.close_time else None,
        "close_reason": t.close_reason, "close_z": t.close_z,
        "close_residual": t.close_residual, "close_prices": t.close_prices,
        # Primary PnL shown in UI (exchange-only when live)
        "pnl": pnl,
        "realized_pnl": realized,
        # model kept for research only; UI should not use it for live totals
        "model_pnl": model,
        "pnl_source": (
            "exchange" if (t.mode == "live" and realized is not None)
            else ("pending_exchange" if t.mode == "live" else "model")
        ),
        "fee_paid": float(t.fee_paid) if t.fee_paid is not None else None,
        "legs_entry": getattr(t, "legs_entry", None),
        "legs_close": getattr(t, "legs_close", None),
        "trade_notional": float(t.trade_notional) if getattr(t, "trade_notional", None) is not None else None,
    }


def _resolve_mode(db: Session, mode: str | None) -> str | None:
    if mode is None:
        state = get_or_create_bot_state(db)
        return state.trading_mode or "paper"
    m = str(mode).lower().strip()
    if m in ("all", "*"):
        return None
    if m in ("paper", "live"):
        return m
    raise HTTPException(400, "mode must be paper, live, or all")


def assign_codenames(db: Session, force_all: bool = False) -> list[dict]:
    groups = db.query(Group).order_by(Group.id).all()
    used = {g.name for g in groups if g.name}
    changes = []
    for g in groups:
        if not force_all and not is_legacy_name(g.name):
            continue
        if g.name in used:
            used.discard(g.name)
        new_name = generate_group_name(used=used)
        used.add(new_name)
        old = g.name
        g.name = new_name
        changes.append({"id": g.id, "old": old, "new": new_name})
    if changes:
        db.commit()
    return changes


@router.get("")
def list_groups(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Group)
    if status:
        q = q.filter_by(status=status)
    return [_group_to_dict(g) for g in q.order_by(Group.created_at.desc()).all()]


@router.post("/assign-codenames")
def api_assign_codenames(force_all: bool = False, db: Session = Depends(get_db)):
    changes = assign_codenames(db, force_all=force_all)
    return {"renamed": len(changes), "changes": changes}


@router.get("/performance/all")
def all_groups_performance(
    mode: str | None = Query(None, description="paper | live | all (default: current bot mode)"),
    db: Session = Depends(get_db),
):
    resolved = _resolve_mode(db, mode)
    out = []
    for g in db.query(Group).filter(Group.status.in_(["active", "inactive"])).all():
        q = db.query(Trade).filter_by(group_id=g.id, status="closed")
        if resolved:
            q = q.filter_by(mode=resolved)
        closed = q.all()
        closed_dicts = [
            {"pnl": _trade_pnl(t), "entry_time": t.entry_time, "close_time": t.close_time, "direction": getattr(t, "direction", None)}
            for t in closed if _trade_pnl(t) is not None
        ]
        perf = compute_group_performance(closed_dicts).as_dict()
        missing_exchange = 0
        if resolved == "live":
            missing_exchange = sum(
                1 for t in closed
                if getattr(t, "realized_pnl", None) is None
            )
        out.append({
            "group_id": g.id,
            "name": g.name or f"group-{g.id}",
            "symbols": g.symbols,
            "dependent_symbol": g.dependent_symbol,
            "status": g.status,
            "source": g.source,
            "sector": g.sector,
            "mode": resolved or "all",
            "pnl_source": "exchange" if resolved == "live" else "model",
            "missing_exchange_pnl_count": missing_exchange,
            **perf,
        })
    return out


@router.get("/{group_id}")
def get_group(group_id: int, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    return _group_to_dict(g)


@router.patch("/{group_id}/status")
def set_group_status(group_id: int, body: GroupStatusUpdate, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    if body.status not in ("active", "inactive", "archived", "candidate"):
        raise HTTPException(400, "invalid status")

    if body.status != "active" and g.status == "active":
        open_n = db.query(Trade).filter_by(group_id=g.id, status="open").count()
        if open_n > 0:
            raise HTTPException(
                409,
                f"Group has {open_n} open trade(s). Close them before setting status to {body.status}.",
            )

    g.status = body.status
    db.commit()
    return _group_to_dict(g)


@router.delete("/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    open_n = db.query(Trade).filter_by(group_id=g.id, status="open").count()
    if open_n > 0:
        raise HTTPException(409, f"Cannot delete group with {open_n} open trade(s)")
    db.delete(g)
    db.commit()
    return {"deleted": group_id}


# One live fit per (group, candle). The OLS only changes when a new candle opens,
# so every viewer/poll inside the same candle is served from memory and no
# exchange request is made. The per-group lock lets concurrent callers share one fit.
_LIVE_FIT_CACHE: dict[int, tuple[tuple, dict]] = {}
_LIVE_FIT_LOCKS: dict[int, asyncio.Lock] = {}


@router.get("/{group_id}/live-fit")
async def live_fit(group_id: int, persist: bool = False, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    backbone = config_service.get_section(db, "backbone")
    sampling_time = max(1, int(backbone.get("sampling_time", 60)))
    key = (
        int(time.time() // sampling_time),
        tuple(g.symbols or ()), g.dependent_symbol,
        int(backbone.get("window_size", 100)),
        int(backbone.get("chart_history_bars", 500) or 0),
        str(backbone.get("stationarity_method", "engle_granger")),
        float(backbone.get("adf_alpha", 0.05)), float(backbone.get("kpss_alpha", 0.05)),
    )
    lock = _LIVE_FIT_LOCKS.setdefault(group_id, asyncio.Lock())
    async with lock:
        hit = _LIVE_FIT_CACHE.get(group_id)
        if hit and hit[0] == key and not persist:
            return {**hit[1], "cached": True}
        payload = await _compute_live_fit(group_id, persist, db)
        _LIVE_FIT_CACHE[group_id] = (key, payload)
        return {**payload, "cached": False}


_MAX_CHART_BARS = 3000


def _frozen_residual_series(price_df, dependent: str, betas: dict, intercept: float) -> list:
    """Residual of EVERY bar in price_df under one fixed set of betas/intercept."""
    cols = [c for c in betas if c in price_df.columns]
    x = sum(float(betas[c]) * price_df[c].to_numpy(dtype=float) for c in cols)
    resid = price_df[dependent].to_numpy(dtype=float) - (float(intercept) + x)
    return [[ts.isoformat(), float(v)] for ts, v in zip(price_df.index, resid)]


def _prices_payload(price_df) -> dict:
    """Raw closes of the shown range, so the browser can recompute residuals for ANY betas
    (a refit, a trade's entry betas, older history) without another exchange request."""
    return {
        "t": [int(ts.value // 10**9) for ts in price_df.index],
        "c": {str(c): [float(v) for v in price_df[c].to_numpy()] for c in price_df.columns},
    }


async def _compute_live_fit(group_id: int, persist: bool, db: Session) -> dict:
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    if not g.symbols or len(g.symbols) < 2:
        raise HTTPException(400, "group needs at least 2 symbols")

    backbone = config_service.get_section(db, "backbone")
    window_size = int(backbone.get("window_size", 100))
    sampling_time = int(backbone.get("sampling_time", 60))
    resolution = seconds_to_resolution(sampling_time)
    history = min(_MAX_CHART_BARS, int(backbone.get("chart_history_bars", 500) or 0))
    bars_needed = max(window_size + 5, history)

    md = factory.build_market_data_client(db)
    try:
        price_df = await fetch_price_df(md, list(g.symbols), resolution, bars_needed)
    finally:
        await md.aclose()

    if price_df.empty or len(price_df) < 30:
        raise HTTPException(
            422,
            f"Insufficient OHLC history for {g.symbols} at resolution={resolution} "
            f"(got {0 if price_df.empty else len(price_df)} bars, need >= 30).",
        )

    effective_window = min(window_size, len(price_df))
    window = price_df.iloc[-effective_window:]
    shown = price_df.iloc[-max(effective_window, history):] if history else window
    dependent = g.dependent_symbol if g.dependent_symbol in window.columns else list(window.columns)[0]
    price_matrix = {s: window[s].to_numpy() for s in window.columns}

    try:
        fit_res = fit_ols(dependent, price_matrix)
    except Exception as e:
        raise HTTPException(422, f"OLS fit failed: {e}") from e

    adf_alpha = float(backbone.get("adf_alpha", 0.05))
    kpss_alpha = float(backbone.get("kpss_alpha", 0.05))
    stat = test_stationarity(
        fit_res.residual, adf_alpha, kpss_alpha,
        y=price_matrix[dependent],
        x_cols=[price_matrix[s] for s in price_matrix if s != dependent],
        method=str(backbone.get("stationarity_method", "engle_granger")),
    )

    residual_series = [[ts.isoformat(), float(v)] for ts, v in zip(window.index, fit_res.residual)]
    # Same betas applied to the whole shown range, so the chart can show more history
    # than the OLS window without changing the fit itself.
    residual_full = (
        _frozen_residual_series(shown, dependent, fit_res.betas, fit_res.intercept)
        if len(shown) > len(window) else residual_series
    )
    fitted_at = dt.datetime.utcnow()

    def _py(x):
        if x is None:
            return None
        if hasattr(x, "item"):
            try:
                return x.item()
            except Exception:
                pass
        try:
            return float(x)
        except Exception:
            return x

    payload = {
        "id": None,
        "group_id": int(g.id),
        "group_name": g.name,
        "dependent_symbol": dependent,
        "fitted_at": fitted_at.isoformat(),
        "window_start": window.index[0].to_pydatetime().isoformat(),
        "window_end": window.index[-1].to_pydatetime().isoformat(),
        "betas": {str(k): float(v) for k, v in fit_res.betas.items()},
        "intercept": float(fit_res.intercept),
        "resid_mean": float(fit_res.resid_mean),
        "resid_std": float(fit_res.resid_std),
        "adf_stat": _py(stat.adf_stat),
        "adf_pvalue": _py(stat.adf_pvalue),
        "kpss_stat": _py(stat.kpss_stat),
        "kpss_pvalue": _py(stat.kpss_pvalue),
        "passed": bool(stat.passed),
        "residual_series": residual_series,
        "residual_series_full": residual_full,
        "prices": _prices_payload(shown),
        "resolution": resolution,
        "bars_used": int(effective_window),
        "sampling_time": int(sampling_time),
    }

    if persist:
        try:
            ols_row = OLSFit(
                group_id=g.id,
                fitted_at=fitted_at,
                window_start=window.index[0].to_pydatetime(),
                window_end=window.index[-1].to_pydatetime(),
                betas=fit_res.betas,
                intercept=fit_res.intercept,
                resid_mean=fit_res.resid_mean,
                resid_std=fit_res.resid_std,
                adf_stat=stat.adf_stat,
                adf_pvalue=stat.adf_pvalue,
                kpss_stat=stat.kpss_stat,
                kpss_pvalue=stat.kpss_pvalue,
                passed=bool(stat.passed),
                residual_series=residual_series,
            )
            db.add(ols_row)
            db.commit()
            db.refresh(ols_row)
            payload["id"] = ols_row.id
        except Exception:
            log.exception("failed to persist live fit for group %s", g.id)
            db.rollback()

    return payload


@router.get("/{group_id}/fits")
def list_fits(group_id: int, limit: int = 200, db: Session = Depends(get_db)):
    fits = (
        db.query(OLSFit).filter_by(group_id=group_id)
        .order_by(OLSFit.fitted_at.desc()).limit(limit).all()
    )
    return [_fit_to_dict(f) for f in reversed(fits)]


@router.get("/{group_id}/fits/latest")
def latest_fit(group_id: int, db: Session = Depends(get_db)):
    f = db.query(OLSFit).filter_by(group_id=group_id).order_by(OLSFit.fitted_at.desc()).first()
    if not f:
        raise HTTPException(404, "no fits yet for this group")
    return _fit_to_dict(f)


@router.get("/{group_id}/fits/{fit_id}")
def get_fit(group_id: int, fit_id: int, db: Session = Depends(get_db)):
    """One stored fit by id (a trade's ols_fit_id may be older than the /fits newest-200 window)."""
    f = db.query(OLSFit).filter_by(id=fit_id, group_id=group_id).first()
    if not f:
        raise HTTPException(404, "fit not found for this group")
    return _fit_to_dict(f)


_MAX_OLDER_BARS = 1000


@router.get("/{group_id}/prices")
async def older_prices(
    group_id: int,
    before: int = Query(..., description="epoch seconds; only bars strictly before this are returned"),
    bars: int = Query(500, ge=10, le=_MAX_OLDER_BARS),
    db: Session = Depends(get_db),
):
    """One chunk of history that ends just before `before` (for scrolling the chart to the left)."""
    g = db.get(Group, group_id)
    if not g or not g.symbols:
        raise HTTPException(404, "group not found")
    backbone = config_service.get_section(db, "backbone")
    resolution = seconds_to_resolution(max(1, int(backbone.get("sampling_time", 60))))
    md = factory.build_market_data_client(db)
    try:
        price_df = await fetch_price_df(md, list(g.symbols), resolution, bars, end_ts=int(before))
    finally:
        await md.aclose()
    if not price_df.empty:
        price_df = price_df[[int(ts.value // 10**9) < int(before) for ts in price_df.index]]
    payload = _prices_payload(price_df) if not price_df.empty else {"t": [], "c": {}}
    payload["exhausted"] = price_df.empty
    return payload


@router.get("/{group_id}/fits/{fit_id}/extended")
async def get_fit_extended(group_id: int, fit_id: int, db: Session = Depends(get_db)):
    """A stored fit's betas applied to the full chart range (not just the fit's own window)."""
    f = db.query(OLSFit).filter_by(id=fit_id, group_id=group_id).first()
    if not f:
        raise HTTPException(404, "fit not found for this group")
    g = db.get(Group, group_id)
    out = _fit_to_dict(f)
    if not g or not f.betas:
        return out
    backbone = config_service.get_section(db, "backbone")
    sampling_time = max(1, int(backbone.get("sampling_time", 60)))
    resolution = seconds_to_resolution(sampling_time)
    history = min(_MAX_CHART_BARS, int(backbone.get("chart_history_bars", 500) or 0))
    # Cover everything since the fit's window began, plus the same history before "now".
    since_bars = 0
    if f.window_start:
        since_bars = int((dt.datetime.utcnow() - f.window_start).total_seconds() // sampling_time) + 5
    bars = min(_MAX_CHART_BARS, max(history, since_bars, int(backbone.get("window_size", 100)) + 5))
    symbols = list(g.symbols or [])
    dependent = g.dependent_symbol if g.dependent_symbol in symbols else symbols[0]
    md = factory.build_market_data_client(db)
    try:
        price_df = await fetch_price_df(md, symbols, resolution, bars)
    finally:
        await md.aclose()
    if price_df.empty or dependent not in price_df.columns:
        return out
    out["residual_series_full"] = _frozen_residual_series(price_df, dependent, f.betas, f.intercept)
    out["prices"] = _prices_payload(price_df)
    out["dependent_symbol"] = dependent
    out["resolution"] = resolution
    out["extended_bars"] = int(len(price_df))
    return out


@router.get("/{group_id}/trades")
def list_trades(
    group_id: int,
    mode: str | None = Query(None, description="paper | live | all (default: current bot mode)"),
    db: Session = Depends(get_db),
):
    resolved = _resolve_mode(db, mode)
    q = db.query(Trade).filter_by(group_id=group_id)
    if resolved:
        q = q.filter_by(mode=resolved)
    trades = q.order_by(Trade.entry_time.desc()).all()
    return [_trade_to_dict(t) for t in trades]


@router.get("/{group_id}/performance")
def group_performance(
    group_id: int,
    mode: str | None = Query(None),
    db: Session = Depends(get_db),
):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    resolved = _resolve_mode(db, mode)
    q = db.query(Trade).filter_by(group_id=group_id, status="closed")
    if resolved:
        q = q.filter_by(mode=resolved)
    closed = q.all()
    closed_dicts = [
        {"pnl": _trade_pnl(t), "entry_time": t.entry_time, "close_time": t.close_time, "direction": getattr(t, "direction", None)}
        for t in closed if _trade_pnl(t) is not None
    ]
    return compute_group_performance(closed_dicts).as_dict()


@router.get("/{group_id}/equity_curve")
async def equity_curve(
    group_id: int,
    mode: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """
    Cumulative equity for the group.

    paper: starts at 0, adds model PnL each close.
    live: starts from current margin wallet free balance minus sum of this
          group's exchange realized PnLs (so the curve ends near account
          level and is grounded in exchange data, not model residual).
    """
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    resolved = _resolve_mode(db, mode)
    q = db.query(Trade).filter_by(group_id=group_id, status="closed")
    if resolved:
        q = q.filter_by(mode=resolved)
    closed = q.order_by(Trade.close_time.asc()).all()

    pnls = []
    for t in closed:
        if t.close_time is None:
            continue
        p = _trade_pnl(t)
        if p is None:
            continue
        pnls.append((t.close_time.isoformat(), float(p)))

    start_equity = 0.0
    source = "model_cum_pnl"
    account_balance = None

    if resolved == "live":
        source = "exchange_realized"
        try:
            client = factory.build_trading_client(db)
            try:
                if not isinstance(client, PaperExchangeClient):
                    from backend.engine import xt_hooks
                    ex_name = (getattr(g, "exchange", None) or "nobitex").lower()
                    account_balance = await xt_hooks.read_free_balance(client, exchange=ex_name)
            finally:
                if not isinstance(client, PaperExchangeClient):
                    await client.aclose()
        except Exception as e:
            log.warning("equity_curve: could not read balance: %s", e)
            account_balance = None

        sum_realized = sum(p for _, p in pnls)
        if account_balance is not None:
            # Reconstruct starting equity so final point ≈ current free balance
            # (this group's contribution only).
            start_equity = float(account_balance) - sum_realized
        else:
            start_equity = 0.0
            source = "exchange_realized_no_balance"

    points = []
    cum = float(start_equity)
    points.append({
        "time": None,
        "equity": cum,
        "cum_pnl": 0.0,
        "pnl": 0.0,
        "is_start": True,
    })
    for ts, p in pnls:
        cum += p
        points.append({
            "time": ts,
            "equity": cum,
            "cum_pnl": cum - float(start_equity),
            "pnl": p,
            "is_start": False,
        })

    return {
        "mode": resolved or "all",
        "source": source,
        "start_equity": start_equity,
        "account_balance": account_balance,
        "points": points,
    }
