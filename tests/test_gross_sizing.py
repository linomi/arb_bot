import asyncio
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config_service
from backend.engine.bot_engine import BotEngine
from backend.models import Base, Group, OLSFit, Trade
from backend.strategy.account import paper_free_balance, trade_gross
from backend.strategy.pnl import legs_gross_notional
from backend.strategy.sizing import ExcessiveScalingError, leg_orders

PRICES = {"ETH": 2500.0, "BTC": 60000.0, "SOL": 150.0}


def test_gross_basis_total_equals_trade_notional():
    legs = leg_orders("ETH", "long_residual", {"BTC": 0.04}, PRICES, 40.0, min_order_value=1.0)
    assert abs(legs_gross_notional(legs) - 40.0) < 1e-9
    eth, btc = legs
    assert eth["side"] == "buy" and btc["side"] == "sell"
    # legs split by beta: BTC leg = 0.04 * 60000 / 2500 = 0.96 x the ETH leg
    assert abs(btc["qty"] * btc["price"] / (eth["qty"] * eth["price"]) - 0.96) < 1e-9
    assert abs(btc["qty"] / eth["qty"] - 0.04) < 1e-12


def test_dependent_basis_keeps_legacy_meaning():
    legs = leg_orders("ETH", "long_residual", {"BTC": 0.04}, PRICES, 40.0,
                      min_order_value=1.0, basis="dependent")
    assert abs(legs[0]["qty"] * legs[0]["price"] - 40.0) < 1e-9
    assert abs(legs_gross_notional(legs) - 40.0 * 1.96) < 1e-9


def test_negligible_leg_dropped_and_gross_renormalised():
    legs = leg_orders("ETH", "short_residual", {"BTC": 0.04, "SOL": 1e-5}, PRICES, 40.0, min_order_value=1.0)
    assert [l["symbol"] for l in legs] == ["ETH", "BTC"]
    assert abs(legs_gross_notional(legs) - 40.0) < 1e-9  # dropped leg does not leak into the total
    assert legs[0]["dropped_legs"][0]["symbol"] == "SOL"


def test_negative_beta_same_side():
    legs = leg_orders("ETH", "long_residual", {"BTC": -0.04}, PRICES, 40.0, min_order_value=1.0)
    assert legs[1]["side"] == "buy"


def test_min_order_scales_gross_up_and_cap_refuses():
    # smallest leg (BTC, ~49% of gross) must reach 10 -> gross scales to ~20.4
    legs = leg_orders("ETH", "long_residual", {"BTC": 0.04}, PRICES, 12.0, min_order_value=10.0)
    notionals = [l["qty"] * l["price"] for l in legs]
    assert min(notionals) >= 10.0 - 1e-9
    assert legs[0]["scaled_for_min"] is True
    with pytest.raises(ExcessiveScalingError):
        leg_orders("ETH", "long_residual", {"BTC": 0.04}, PRICES, 12.0, min_order_value=10.0, max_scale=1.2)


def test_unknown_basis_rejected():
    with pytest.raises(ValueError):
        leg_orders("ETH", "long_residual", {"BTC": 0.04}, PRICES, 40.0, basis="nope")


# ------------------------------------------------------------------ paper wallet
def _db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    config_service.seed_defaults_if_missing(db)
    return db


def _group(db, dep="ETH/USDT:USDT", ex="xt"):
    g = Group(name="g", dependent_symbol=dep, symbols=[dep, "BTC/USDT:USDT"], exchange=ex)
    db.add(g); db.flush()
    f = OLSFit(group_id=g.id, betas={}, intercept=0.0, resid_mean=0.0, resid_std=1.0)
    db.add(f); db.flush()
    return g, f


def _trade(db, g, f, status, gross, pnl=None, mode="paper"):
    db.add(Trade(group_id=g.id, ols_fit_id=f.id, direction="long_residual", mode=mode,
                 entry_time=dt.datetime(2026, 1, 1), entry_z=2, entry_residual=0, entry_prices={},
                 status=status, pnl=pnl, trade_notional=gross, notional_basis="gross",
                 close_time=dt.datetime(2026, 1, 1, 2) if status == "closed" else None))


def test_paper_free_balance_start_pnl_and_margin():
    db = _db()
    g, f = _group(db)
    _trade(db, g, f, "closed", 40.0, pnl=5.0)
    _trade(db, g, f, "closed", 40.0, pnl=-2.0)
    _trade(db, g, f, "open", 60.0)           # xt_leverage default 2 -> 30 margin
    _trade(db, g, f, "open", 999.0, mode="live")  # live rows never touch the paper wallet
    db.commit()
    bb = config_service.get_section(db, "backbone")
    assert paper_free_balance(db, bb) == 1000.0 + 3.0 - 30.0


def test_paper_free_balance_disabled_and_ignores_nobitex():
    db = _db()
    g, f = _group(db, dep="BTC-IRT", ex="nobitex")
    _trade(db, g, f, "open", 5e7)
    db.commit()
    bb = config_service.get_section(db, "backbone")
    assert paper_free_balance(db, bb) == 1000.0   # IRT trades are not in the USDT wallet
    assert paper_free_balance(db, {**bb, "paper_start_balance": 0}) is None


def test_trade_gross_legacy_vs_gross_rows():
    db = _db()
    g, f = _group(db)
    f.betas = {"BTC/USDT:USDT": 0.04}
    t = Trade(group_id=g.id, ols_fit_id=f.id, direction="long_residual", mode="paper",
              entry_time=dt.datetime(2026, 1, 1), entry_z=2, entry_residual=0,
              entry_prices={"ETH/USDT:USDT": 2500.0, "BTC/USDT:USDT": 60000.0},
              status="open", trade_notional=40.0, notional_basis=None)
    db.add(t); db.commit()
    assert abs(trade_gross(t) - 40.0 * 1.96) < 1e-6   # legacy: dependent-leg basis
    t.notional_basis = "gross"
    assert trade_gross(t) == 40.0


# ------------------------------------------------------------------ balance fit
class _C:  # trading client stand-in that is neither Nobitex nor XT
    async def get_min_notional(self, s):
        return 5.0


def _fit(**kw):
    eng = BotEngine()
    return asyncio.run(eng._fit_notional_to_balance(
        _C(), dependent_symbol="ETH/USDT:USDT", direction="long_residual",
        betas={"BTC/USDT:USDT": 0.04},
        prices={"ETH/USDT:USDT": 2500.0, "BTC/USDT:USDT": 60000.0},
        trading_mode="paper", exchange="xt", symbols=["ETH/USDT:USDT", "BTC/USDT:USDT"], **kw))


def test_paper_fit_no_limit_when_disabled():
    legs, n, msg = _fit(desired_notional=500.0)
    assert n == 500.0 and abs(legs_gross_notional(legs) - 500.0) < 1e-9


def test_paper_fit_scales_down_to_wallet():
    # wallet 50 at 2x: usable = 50*0.85 - buffer(1.0) = 41.5 USDT margin -> 83 gross max
    legs, n, msg = _fit(desired_notional=500.0, paper_free=50.0, paper_leverage=2.0)
    assert legs is not None and n < 500.0
    margin = legs_gross_notional(legs) / 2.0
    assert margin <= 50.0 * 0.85
    assert "scaled" in msg


def test_paper_fit_refuses_when_wallet_empty():
    legs, n, msg = _fit(desired_notional=40.0, paper_free=3.0, paper_leverage=2.0)
    assert legs is None and "balance too low" in msg
