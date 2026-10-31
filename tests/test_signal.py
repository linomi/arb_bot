import sys

import numpy as np
import pandas as pd
import pytest

from backend.engine.backtester import backtest_group
from backend.strategy.ols import fit_ols
from backend.strategy.signal import (
    ev_check, group_win_rate, half_life_limit_bars, regime_block, scored_fit, signal_cfg_from_params,
    window_bars, z_cap_block,
)

Y, X = "Y", "X"


def _pair(n=201, phi=0.5, seed=3, noise=1.0):
    r = np.random.default_rng(seed)
    x = 100 + np.cumsum(r.normal(0, 1, n))
    ou = np.zeros(n)
    for i in range(1, n):
        ou[i] = phi * ou[i - 1] + r.normal(0, noise)
    return {Y: 2.0 * x + 5 + ou, X: x}


def test_oos_scores_the_last_bar_against_a_fit_that_excludes_it():
    pm = _pair()
    sf = scored_fit(Y, pm, "oos")
    ref = fit_ols(Y, {k: v[:-1] for k, v in pm.items()})
    assert sf.fit.betas[X] == pytest.approx(ref.betas[X])
    assert sf.resid_now == pytest.approx(pm[Y][-1] - (ref.intercept + ref.betas[X] * pm[X][-1]))
    assert len(sf.residual) == len(pm[Y]) and sf.residual[-1] == pytest.approx(sf.resid_now)
    assert len(sf.y) == len(pm[Y]) - 1
    # prediction-error std is wider than the in-sample residual std
    assert sf.std > ref.resid_std


def test_in_sample_method_is_the_legacy_fit():
    pm = {k: v[:-1] for k, v in _pair().items()}
    sf = scored_fit(Y, pm, "in_sample")
    ref = fit_ols(Y, pm)
    assert sf.resid_now == pytest.approx(ref.residual[-1]) and sf.std == pytest.approx(ref.resid_std)
    assert window_bars(200, "in_sample") == 200 and window_bars(200, "oos") == 201


def test_in_sample_fit_hides_part_of_a_last_bar_spike():
    pm = _pair()
    pm[Y] = pm[Y].copy()
    pm[Y][-1] += 6.0
    assert abs(scored_fit(Y, pm, "oos").z) > abs(scored_fit(Y, {k: v for k, v in pm.items()}, "in_sample").z)


def test_ev_check_prices_in_the_stop():
    # sigma_rel 1%: gain = 1%*2 - 0.2% = 1.8%, loss = 1%*2.5 + 0.2% = 2.7%
    ok, d = ev_check(z_now=2.5, sigma_rel=0.01, z_close=0.5, z_stop=4.5, cost_rate=0.001,
                     win_rate=0.65, stop_overshoot=0.5)
    assert ok and d["ev_frac"] == pytest.approx(0.65 * 0.018 - 0.35 * 0.027)
    # 1-minute-like numbers: sigma_rel 0.13% with 0.11% per side costs never pays
    ok, d = ev_check(z_now=3.0, sigma_rel=0.0013, z_close=0.5, z_stop=4.5, cost_rate=0.0011,
                     win_rate=0.65)
    assert not ok and "expected value" in d["reject_reason"]


def test_group_win_rate_is_shrunk_toward_the_default():
    assert group_win_rate(None, 0.65) == 0.65
    assert group_win_rate({"trade_count": 0, "win_rate": 0.9}, 0.65) == 0.65
    assert group_win_rate({"trade_count": 20, "win_rate": 0.85}, 0.65) == pytest.approx(0.75)
    assert group_win_rate({"trade_count": 20, "win_rate": 0.45}, 0.65) == pytest.approx(0.55)


def test_regime_block_flags_a_jump_but_not_normal_moves():
    cfg = signal_cfg_from_params({})
    r = np.random.default_rng(0).normal(0, 1, 120)
    assert regime_block(r, cfg) is None
    jumped = r.copy()
    jumped[-1] = jumped[-2] + 10.0
    assert regime_block(jumped, cfg).startswith("jump")
    assert regime_block(jumped, {**cfg, "jump_max_sigma": 0, "vol_regime_max": 0}) is None


def test_regime_block_flags_a_volatility_burst():
    cfg = {**signal_cfg_from_params({}), "jump_max_sigma": 0}
    rng = np.random.default_rng(1)
    r = np.cumsum(np.concatenate([rng.normal(0, 1, 150), rng.normal(0, 4, 20)]))
    assert regime_block(r, cfg).startswith("volatility regime")


def test_z_cap():
    cfg = signal_cfg_from_params({"z_entry_max": 3.5})
    assert z_cap_block(-3.6, cfg) and z_cap_block(3.4, cfg) is None
    assert z_cap_block(9.0, signal_cfg_from_params({"z_entry_max": 0})) is None


def test_half_life_limit():
    r = np.zeros(400)
    rng = np.random.default_rng(2)
    for i in range(1, 400):
        r[i] = 0.5 * r[i - 1] + rng.normal()
    lim = half_life_limit_bars(r, {"max_holding_halflives": 3})
    assert 1.5 < lim < 4.5  # half-life of AR(0.5) is 1 bar
    assert half_life_limit_bars(r, {"max_holding_halflives": 0}) is None
    assert half_life_limit_bars(np.arange(100.0), {"max_holding_halflives": 3}) is None  # not mean-reverting


def _df(n=1500, phi=0.9, seed=4):
    pm = _pair(n=n, phi=phi, seed=seed, noise=0.6)
    return pd.DataFrame(pm, index=pd.date_range("2026-01-01", periods=n, freq="15min"))[[Y, X]]


_BT = dict(dependent_symbol=Y, window_size=200, adf_alpha=1.0, kpss_alpha=0.0, z_entry=2.0, z_close=0.5,
           z_stop_loss=4.5, transaction_cost_rate=0.0, half_life_max_fraction=0)


def test_backtest_runs_with_signal_settings_and_records_half_life_stops():
    sig = {**signal_cfg_from_params({}), "ev_gate": False, "max_holding_halflives": 0.5}
    res = backtest_group(_df(), signal=sig, **_BT)
    reasons = {t.close_reason for t in res.trades}
    assert res.trades and "half_life_stop" in reasons


def test_backtest_without_signal_keeps_the_legacy_replay():
    df = _df()
    legacy = backtest_group(df, **_BT)
    explicit = backtest_group(df, signal={**signal_cfg_from_params({}), "zscore_method": "in_sample",
                                          "ev_gate": False, "jump_max_sigma": 0, "vol_regime_max": 0,
                                          "max_holding_halflives": 0}, **_BT)
    assert [(t.entry_idx, t.exit_idx) for t in legacy.trades] == [(t.entry_idx, t.exit_idx) for t in explicit.trades]


def test_config_validation_of_new_keys():
    from backend.config_service import _validate_backbone
    base = {"z_entry": 2.5, "z_close": 0.5, "z_stop_loss": 4.5}
    _validate_backbone({**base, "zscore_method": "oos", "z_entry_max": 3.5, "ev_win_rate": 0.65})
    for bad in ({"zscore_method": "x"}, {"z_entry_max": 2.0}, {"ev_win_rate": 1.5}, {"jump_bars": 0},
                {"vol_regime_max": -1}):
        with pytest.raises(ValueError):
            _validate_backbone({**base, **bad})


def test_apply_recommended_settings_script(monkeypatch, capsys):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend import config_service, db as dbmod
    from backend.models import Base
    import importlib.util
    from pathlib import Path

    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    sf = sessionmaker(bind=eng)
    s = sf()
    config_service.seed_defaults_if_missing(s)
    config_service.update_section(s, "backbone", {"sampling_time": 60, "z_stop_loss": 8, "stationarity_method": "adf_kpss"})
    s.close()
    monkeypatch.setattr(dbmod, "SessionLocal", sf)
    spec = importlib.util.spec_from_file_location(
        "apply_settings", Path(__file__).resolve().parent.parent / "scripts" / "apply_recommended_settings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(sys, "argv", ["x"])
    mod.main()
    assert "dry run" in capsys.readouterr().out
    s = sf()
    assert config_service.get_section(s, "backbone")["sampling_time"] == 60
    s.close()

    monkeypatch.setattr(sys, "argv", ["x", "--apply"])
    mod.main()
    s = sf()
    bb = config_service.get_section(s, "backbone")
    assert bb["sampling_time"] == 900 and bb["z_stop_loss"] == 4.5 and bb["stationarity_method"] == "engle_granger"
    assert config_service.get_section(s, "init")["require_oos_positive"] is True
    s.close()
    mod.main()
    assert "already up to date" in capsys.readouterr().out
