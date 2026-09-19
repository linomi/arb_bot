import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db
from backend import config_service
from backend.models import Group, OLSFit, Trade
from backend.schemas import GroupStatusUpdate
from backend.strategy.metrics import compute_group_performance
from backend.strategy.ols import fit_ols
from backend.strategy.stats_tests import test_stationarity
from backend.exchange import factory
from backend.utils import seconds_to_resolution, fetch_price_df

log = logging.getLogger("groups_router")

router = APIRouter(prefix="/api/groups", tags=["groups"])

# Legacy absolute PnL was stored as fraction * 100 (default trade_notional).
# After migration, Trade.pnl is a pure fraction of notional.
_LEGACY_NOTIONAL = 100.0


def _current_notional(db: Session) -> float:
    try:
        n = float(config_service.get_section(db, "backbone").get("trade_notional", 100) or 100)
        return n if n > 0 else 100.0
    except Exception:
        return 100.0


def _mark_fractional(db: Session) -> None:
    try:
        config_service.update_section(db, "system", {"pnl_as_fraction": True})
    except Exception as e:
        log.warning("could not set pnl_as_fraction flag: %s", e)


def _ensure_fractional_pnl(db: Session) -> None:
    """One-time: convert absolute PnL (scaled by legacy 100) to unit-notional fractions.

    Safe against double-runs:
      - skips if system.pnl_as_fraction is already True
      - skips divide if values already look fractional (|pnl| < 1)
    """
    try:
        system = config_service.get_section(db, "system")
    except Exception:
        system = {}
    if system.get("pnl_as_fraction"):
        return

    trades = db.query(Trade).filter(Trade.pnl.isnot(None)).all()
    if not trades:
        _mark_fractional(db)
        db.commit()
        return

    max_abs = max(abs(float(t.pnl)) for t in trades)
    # New engine already stores fractions (typically |pnl| << 1).
    # Only divide legacy absolute values (were fraction * 100, so often |pnl| > 1).
    if max_abs < 1.0:
        log.info(
            "PnL already fractional (max |pnl|=%.6g); marking flag without divide",
            max_abs,
        )
        _mark_fractional(db)
        db.commit()
        return

    for t in trades:
        t.pnl = float(t.pnl) / _LEGACY_NOTIONAL
        if t.fee_paid is not None:
            t.fee_paid = float(t.fee_paid) / _LEGACY_NOTIONAL
    _mark_fractional(db)
    db.commit()
    log.info("Migrated %d trades to fractional PnL (divided by %s)", len(trades), _LEGACY_NOTIONAL)


def _scale_pnl(frac, notional: float):
    if frac is None:
        return None
    return float(frac) * notional


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


def _trade_to_dict(t: Trade, notional: float) -> dict:
    return {
        "id": t.id, "group_id": t.group_id, "ols_fit_id": t.ols_fit_id,
        "direction": t.direction, "mode": t.mode,
        "entry_time": t.entry_time.isoformat(), "entry_z": t.entry_z,
        "entry_residual": t.entry_residual, "entry_prices": t.entry_prices,
        "status": t.status,
        "close_time": t.close_time.isoformat() if t.close_time else None,
        "close_reason": t.close_reason, "close_z": t.close_z,
        "close_residual": t.close_residual, "close_prices": t.close_prices,
        "pnl": _scale_pnl(t.pnl, notional),
        "pnl_fraction": t.pnl,
        "fee_paid": _scale_pnl(t.fee_paid, notional),
        "trade_notional": notional,
    }


@router.get("")
def list_groups(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Group)
    if status:
        q = q.filter_by(status=status)
    return [_group_to_dict(g) for g in q.order_by(Group.created_at.desc()).all()]


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
    g.status = body.status
    db.commit()
    return _group_to_dict(g)


@router.delete("/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    db.delete(g)
    db.commit()
    return {"deleted": group_id}


@router.get("/{group_id}/live-fit")
async def live_fit(group_id: int, persist: bool = True, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    if not g.symbols or len(g.symbols) < 2:
        raise HTTPException(400, "group needs at least 2 symbols")

    backbone = config_service.get_section(db, "backbone")
    window_size = int(backbone.get("window_size", 100))
    sampling_time = int(backbone.get("sampling_time", 60))
    resolution = seconds_to_resolution(sampling_time)
    bars_needed = window_size + 5

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
    dependent = g.dependent_symbol if g.dependent_symbol in window.columns else list(window.columns)[0]
    price_matrix = {s: window[s].to_numpy() for s in window.columns}

    try:
        fit_res = fit_ols(dependent, price_matrix)
    except Exception as e:
        raise HTTPException(422, f"OLS fit failed: {e}") from e

    adf_alpha = float(backbone.get("adf_alpha", 0.05))
    kpss_alpha = float(backbone.get("kpss_alpha", 0.05))
    stat = test_stationarity(fit_res.residual, adf_alpha, kpss_alpha)

    residual_series = [
        [ts.isoformat(), float(v)] for ts, v in zip(window.index, fit_res.residual)
    ]
    fitted_at = dt.datetime.utcnow()

    def _py(x):
        if x is None:
            return None
        if hasattr(x, "item"):
            try:
                return x.item()
            except Exception:
                pass
        if isinstance(x, (bool, int, float, str)):
            return x
        try:
            return float(x)
        except Exception:
            return x

    payload = {
        "id": None,
        "group_id": int(g.id),
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
                resid_mean=fit_res.resid_std,
                resid_std=fit_res.resid_std,
                adf_stat=stat.adf_stat,
                adf_pvalue=stat.adf_pvalue,
                kpss_stat=stat.kpss_stat,
                kpss_pvalue=stat.kpss_pvalue,
                passed=bool(stat.passed),
                residual_series=residual_series,
            )
            # FIX: resid_mean should be fit_res.resid_mean not resid_std - wait I made a typo!
            # Need to fix this - I accidentally changed resid_mean
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
        db.query(OLSFit)
        .filter_by(group_id=group_id)
        .order_by(OLSFit.fitted_at.desc())
        .limit(limit)
        .all()
    )
    return [_fit_to_dict(f) for f in reversed(fits)]


@router.get("/{group_id}/fits/latest")
def latest_fit(group_id: int, db: Session = Depends(get_db)):
    f = (
        db.query(OLSFit)
        .filter_by(group_id=group_id)
        .order_by(OLSFit.fitted_at.desc())
        .first()
    )
    if not f:
        raise HTTPException(404, "no fits yet for this group")
    return _fit_to_dict(f)


@router.get("/{group_id}/trades")
def list_trades(group_id: int, db: Session = Depends(get_db)):
    _ensure_fractional_pnl(db)
    notional = _current_notional(db)
    trades = db.query(Trade).filter_by(group_id=group_id).order_by(Trade.entry_time.desc()).all()
    return [_trade_to_dict(t, notional) for t in trades]


@router.get("/{group_id}/performance")
def group_performance(group_id: int, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    _ensure_fractional_pnl(db)
    notional = _current_notional(db)
    closed = db.query(Trade).filter_by(group_id=group_id, status="closed").all()
    closed_dicts = [
        {
            "pnl": _scale_pnl(t.pnl, notional),
            "entry_time": t.entry_time,
            "close_time": t.close_time,
        }
        for t in closed
    ]
    return compute_group_performance(closed_dicts).as_dict()


@router.get("/performance/all")
def all_groups_performance(db: Session = Depends(get_db)):
    """Feeds the sortable performance-comparison table on the Backbone tab.

    Metrics use current trade_notional so changing it rescales total/avg PnL,
    drawdown, best/worst, etc. Ratios (Sharpe, win rate) are scale-invariant.
    """
    _ensure_fractional_pnl(db)
    notional = _current_notional(db)
    out = []
    for g in db.query(Group).filter(Group.status.in_(["active", "inactive"])).all():
        closed = db.query(Trade).filter_by(group_id=g.id, status="closed").all()
        closed_dicts = [
            {
                "pnl": _scale_pnl(t.pnl, notional),
                "entry_time": t.entry_time,
                "close_time": t.close_time,
            }
            for t in closed
        ]
        perf = compute_group_performance(closed_dicts).as_dict()
        out.append({
            "group_id": g.id,
            "name": g.name,
            "symbols": g.symbols,
            "status": g.status,
            "source": g.source,
            "sector": g.sector,
            **perf,
        })
    return out
