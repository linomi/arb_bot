import asyncio
import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config_service
from backend.models import Base, Group, OLSFit, Trade
from backend.routers import groups_router as gr


def _db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _trade(db, g, fit, i, pnl, closed=True, gross=100.0):
    t0 = dt.datetime(2026, 1, 1) + dt.timedelta(hours=3 * i)
    legs = [{"symbol": "A", "side": "buy", "qty": 1.0, "price": gross * 0.6, "filled_qty": 1.0},
            {"symbol": "B", "side": "sell", "qty": 1.0, "price": gross * 0.4, "filled_qty": 1.0}]
    db.add(Trade(group_id=g.id, ols_fit_id=fit.id, direction="long_residual", mode="paper",
                 entry_time=t0, entry_z=2, entry_residual=0, entry_prices={},
                 status="closed" if closed else "open",
                 close_time=t0 + dt.timedelta(hours=1) if closed else None,
                 pnl=pnl, model_pnl=pnl, legs_entry=legs, trade_notional=gross))


def test_trade_dict_has_gross_and_margin():
    t = Trade(id=1, group_id=1, ols_fit_id=1, direction="long_residual", mode="paper",
              entry_time=dt.datetime(2026, 1, 1), entry_z=2, entry_residual=0, entry_prices={},
              status="open", legs_entry=[{"symbol": "A", "qty": 2.0, "price": 50.0, "filled_qty": 2.0}])
    d = gr._trade_to_dict(t, leverage=4.0)
    assert d["gross_notional"] == 100.0 and d["margin_used"] == 25.0 and d["leverage"] == 4.0


def test_leverage_only_applies_to_xt():
    assert gr._leverage_for("xt", {"xt_leverage": 3}) == 3.0
    assert gr._leverage_for("nobitex", {"xt_leverage": 3}) == 1.0


def test_paper_equity_starts_at_start_balance_with_margin_series():
    db = _db()
    config_service.seed_defaults_if_missing(db)
    g = Group(name="g", dependent_symbol="A", symbols=["A", "B"], exchange="xt") \
        if "symbols" in Group.__table__.columns else Group(name="g", dependent_symbol="A", exchange="xt")
    db.add(g); db.flush()
    fit = OLSFit(group_id=g.id, betas={}, intercept=0.0, resid_mean=0.0, resid_std=1.0)
    db.add(fit); db.flush()
    _trade(db, g, fit, 0, 10.0)
    _trade(db, g, fit, 1, -4.0)
    _trade(db, g, fit, 2, 0.0, closed=False)
    db.commit()
    out = asyncio.run(gr.equity_curve(g.id, mode="paper", db=db))
    assert out["start_equity"] == 1000.0
    eq = [p["equity"] for p in out["points"]]
    assert eq == [1000.0, 1010.0, 1006.0]
    assert out["leverage"] == 2.0 and out["peak_margin"] == 50.0 and out["current_margin"] == 50.0
    assert abs(out["return_pct"] - 0.6) < 1e-9
    assert abs(out["max_drawdown_pct"] - (4 / 1010 * 100)) < 1e-9


def test_xt_symbol_gets_leverage_even_if_exchange_column_says_nobitex():
    assert gr._leverage_for("nobitex", {"xt_leverage": 2}, "BTC/USDT:USDT") == 2.0


def test_clear_trades_keeps_open_trades():
    db = _db()
    config_service.seed_defaults_if_missing(db)
    g = Group(name="g", dependent_symbol="A", symbols=["A", "B"], exchange="xt")
    db.add(g); db.flush()
    fit = OLSFit(group_id=g.id, betas={}, intercept=0.0, resid_mean=0.0, resid_std=1.0)
    db.add(fit); db.flush()
    _trade(db, g, fit, 0, 1.0)
    _trade(db, g, fit, 1, 2.0)
    _trade(db, g, fit, 2, 0.0, closed=False)
    db.commit()
    out = gr.clear_trades(g.id, mode="paper", db=db)
    assert out["deleted"] == 2 and out["kept_open"] == 1
    assert db.query(Trade).count() == 1
