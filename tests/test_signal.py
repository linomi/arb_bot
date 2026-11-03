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


def test_oos_leverage_is_accurate_for_mixed_price_scales():
    """A $0.0015 coin regressed with a $2,500 coin: X'X is too ill-conditioned for pinv(X'X); the SVD of X is not."""
    r = np.random.default_rng(5)
    n = 201
    cols = {s: p * np.exp(np.cumsum(r.normal(0, 0.004, n))) for s, p in (("A", 0.0015), ("B", 0.0015), ("C", 2500.0))}
    sf = scored_fit("A", cols, "oos")
    X = np.column_stack([np.ones(n - 1), cols["B"][:-1], cols["C"][:-1]])
    x0 = np.array([1.0, cols["B"][-1], cols["C"][-1]])
    d = np.abs(X).max(axis=0)
    _, R = np.linalg.qr(X / d)
    lev_ref = np.sum(np.linalg.solve(R.T, x0 / d) ** 2)
    sigma = np.sqrt(sf.fit.residual @ sf.fit.residual / (n - 1 - 3))
    assert sf.std == pytest.approx(sigma * np.sqrt(1 + lev_ref), rel=1e-6)
    ref = fit_ols("A", {k: v[:-1] for k, v in cols.items()})
    assert sf.fit.betas["C"] == pytest.approx(ref.betas["C"], rel=1e-6)


def test_init_history_is_not_cut_by_a_recently_listed_coin(monkeypatch):
    from backend.engine import init_methods as im
    n = 600
    t = [1_700_000_000 + 900 * i for i in range(n)]
    full = {"t": t, "c": list(np.linspace(1, 2, n))}
    late = {"t": t[-100:], "c": list(np.linspace(1, 2, 100))}
    df = im._align_price_df({"A": full, "B": full, "NEW": late})
    assert len(df) == n and df["NEW"].isna().sum() == n - 100

    seen = []

    def fake_bt(price_df, **kw):
        seen.append((kw["dependent_symbol"], len(price_df)))
        raise RuntimeError("stop")

    monkeypatch.setattr(im, "backtest_group", fake_bt)
    params = {**signal_cfg_from_params({}), "window_size": 200, "adf_alpha": 0.05, "kpss_alpha": 0.05,
              "z_entry": 2.5, "z_close": 0.5, "z_stop_loss": 4.5, "transaction_fee_rate": 0.001,
              "try_all_dependents": True}
    im._evaluate_job((("A", "B"), "random", None), df, params)
    assert seen and all(rows == int(n * 0.7) for _, rows in seen)  # the in-sample 70% of the FULL history


def test_held_out_backtest_runs_only_for_the_kept_dependent(monkeypatch):
    from types import SimpleNamespace
    from backend.engine import init_methods as im
    from backend.strategy.metrics import compute_group_performance
    n = 600
    df = pd.DataFrame({s: np.linspace(1, 2, n) for s in ("A", "B", "C")},
                      index=pd.date_range("2026-01-01", periods=n, freq="15min"))
    calls = []
    n_trades = {"A": 1, "B": 3, "C": 2}  # the score grows with the number of winning trades

    def fake_bt(price_df, **kw):
        dep = kw["dependent_symbol"]
        calls.append((dep, len(price_df)))
        perf = compute_group_performance([{"pnl": 1.0, "entry_time": price_df.index[2 * k],
                                           "close_time": price_df.index[2 * k + 1], "direction": "long_residual"}
                                          for k in range(n_trades[dep])])
        return SimpleNamespace(performance=perf, rounding=None,
                               trades=[SimpleNamespace(pnl=1.0)] * n_trades[dep])

    monkeypatch.setattr(im, "backtest_group", fake_bt)
    params = {**signal_cfg_from_params({}), "window_size": 100, "adf_alpha": 0.05, "kpss_alpha": 0.05,
              "z_entry": 2.5, "z_close": 0.5, "z_stop_loss": 4.5, "transaction_fee_rate": 0.001,
              "try_all_dependents": True}
    res = im._evaluate_job((("A", "B", "C"), "random", None), df, params)
    is_rows = int(n * 0.7)
    assert [c for c in calls if c[1] == is_rows] == [("A", is_rows), ("B", is_rows), ("C", is_rows)]
    assert [c[0] for c in calls if c[1] != is_rows] == ["B"]  # held-out run only for the best dependent
    assert res["dependent_symbol"] == "B" and res["oos_metrics"] is not None


def test_import_groups_script(monkeypatch, capsys, tmp_path):
    import importlib.util
    import json
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend import db as dbmod
    from backend.models import Base, Group

    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    sf = sessionmaker(bind=eng)
    s = sf()
    s.add(Group(name="old", symbols=["B", "A"], dependent_symbol="A", exchange="xt", status="active"))
    s.commit(); s.close()
    monkeypatch.setattr(dbmod, "SessionLocal", sf)
    f = tmp_path / "groups.json"
    f.write_text(json.dumps({"exchange": "xt", "backbone": {"sampling_time": 900}, "groups": [
        {"symbols": ["A", "B"], "dependent_symbol": "A", "source": "random", "backtest_metrics": {"trade_count": 3}},
        {"symbols": ["C", "D", "E"], "dependent_symbol": "D", "source": "random", "backtest_metrics": {"trade_count": 9}},
    ]}))
    spec = importlib.util.spec_from_file_location(
        "import_groups", Path(__file__).resolve().parent.parent / "scripts" / "import_groups.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(sys, "argv", ["x", str(f)])
    mod.main()
    assert "dry run: 1 group(s)" in capsys.readouterr().out
    s = sf(); assert s.query(Group).count() == 1; s.close()

    monkeypatch.setattr(sys, "argv", ["x", str(f), "--apply"])
    mod.main()
    s = sf()
    new = s.query(Group).filter(Group.name != "old").all()
    assert len(new) == 1 and new[0].status == "candidate" and new[0].dependent_symbol == "D"
    assert new[0].exchange == "xt" and new[0].params_snapshot == {"sampling_time": 900}
    assert new[0].backtest_metrics == {"trade_count": 9}
    assert s.query(Group).filter_by(name="old").one().status == "active"  # existing group untouched
    s.close()


def test_require_oos_positive_reaches_the_init_search():
    from backend.routers.init_router import _backbone_params
    bb = {"window_size": 200, "adf_alpha": 0.05, "kpss_alpha": 0.05, "z_entry": 2.5, "z_close": 0.5,
          "z_stop_loss": 4.5}
    assert _backbone_params(bb, "xt", {"require_oos_positive": True})["require_oos_positive"] is True
    assert _backbone_params(bb, "xt")["require_oos_positive"] is False  # manual groups: never rejected


def test_init_score_counts_trades():
    from types import SimpleNamespace
    from backend.engine.init_methods import _score_group

    def score(pnls):
        return _score_group([SimpleNamespace(pnl=p) for p in pnls])

    assert score([]) == -1e9
    assert score([1.0]) == pytest.approx(1.0)
    assert score([1.0, 1.0]) < score([1.0, 1.0, 1.0]) < score([1.0] * 10)  # more winners = more evidence
    assert score([1.0] * 10) == pytest.approx(5.70, abs=0.01)
    assert score([1.0, 1.0]) > score([1.0] * 7 + [-1.0] * 3)  # 7/10 is barely stronger than chance
    assert score([1.0] * 12 + [-1.0] * 3) > score([1.0, 1.0, 1.0])  # ... 12/15 is
    assert score([0.001] * 5) == pytest.approx(score([1000.0] * 5))  # scale-free across price levels
    assert score([-1.0, -1.0]) < 0


def test_min_trades_drops_thin_groups(monkeypatch):
    from types import SimpleNamespace
    from backend.engine import init_methods as im
    from backend.routers.init_router import _backbone_params
    from backend.strategy.metrics import compute_group_performance
    n = 600
    df = pd.DataFrame({s: np.linspace(1, 2, n) for s in ("A", "B")},
                      index=pd.date_range("2026-01-01", periods=n, freq="15min"))

    def fake_bt(price_df, **kw):
        trades = [{"pnl": 1.0, "entry_time": price_df.index[2 * k], "close_time": price_df.index[2 * k + 1]}
                  for k in range(2)]
        return SimpleNamespace(performance=compute_group_performance(trades), rounding=None,
                               trades=[SimpleNamespace(pnl=1.0)] * 2)

    monkeypatch.setattr(im, "backtest_group", fake_bt)
    bb = {"window_size": 100, "adf_alpha": 0.05, "kpss_alpha": 0.05, "z_entry": 2.5, "z_close": 0.5,
          "z_stop_loss": 4.5}
    job = (("A", "B"), "random", None)
    assert im._evaluate_job(job, df, _backbone_params(bb, "xt", {"min_trades": 3})) is None
    kept = im._evaluate_job(job, df, _backbone_params(bb, "xt", {"min_trades": 2}))
    assert kept is not None and kept["score"] == pytest.approx(1.58, abs=0.01)
    assert _backbone_params(bb, "xt")["min_trades"] == 0  # manual groups: never dropped


def _corr_world(n=2000, seed=1):
    """A ~ B + C (three related coins), D unrelated. Prices from random-walk log returns."""
    r = np.random.default_rng(seed)
    b, c, d = (r.normal(0, 0.01, n) for _ in range(3))
    a = 0.6 * b + 0.6 * c + r.normal(0, 0.003, n)
    t = [1_700_000_000 + 900 * i for i in range(n)]
    return {s: {"t": t, "c": list(100 * np.exp(np.cumsum(x)))} for s, x in (("A", a), ("B", b), ("C", c), ("D", d))}


def test_correlation_candidates_grow_groups_by_joint_r2():
    from backend.engine import init_methods as im
    df = im._align_price_df(_corr_world())
    cands = dict(im.correlation_candidates(df, min_group_size=2, max_group_size=3, min_r2=0.5, top_hedges=2))
    assert ("A", "B", "C") in cands or ("A", "C", "B") in cands  # the pair hedges together explain A
    abc = cands.get(("A", "B", "C"), cands.get(("A", "C", "B")))
    assert abc == pytest.approx(0.72 / 0.81, abs=0.03)
    # B or C alone explains only 0.36/0.81 = 44% of A: no A pair passes, only the three together do
    assert not [syms for syms in cands if syms[0] == "A" and len(syms) == 2]
    assert all("D" not in syms for syms in cands)  # unrelated coin: neither target nor hedge
    # each group's first symbol is its target; sizes respect the limits
    assert all(2 <= len(syms) <= 3 for syms in cands)
    only_pairs = im.correlation_candidates(df, 2, 2, 0.4, 2)
    assert {syms for syms, _ in only_pairs} == {("A", "B"), ("B", "A"), ("A", "C"), ("C", "A")}


def test_correlation_uses_in_sample_bars_only():
    from backend.engine import init_methods as im
    world = _corr_world()
    n = len(world["A"]["c"])
    # break D into a perfect copy of A in the held-out 30% only: it must not be picked
    a = np.array(world["A"]["c"])
    world["D"]["c"] = list(np.where(np.arange(n) >= int(n * 0.7), a * 0.5, world["D"]["c"]))
    cands = im.correlation_candidates(im._align_price_df(world), 2, 3, 0.5, 3)
    assert all("D" not in syms for syms, _ in cands)


def test_correlation_init_backtests_the_target_as_dependent(monkeypatch):
    from types import SimpleNamespace
    from backend.engine import init_methods as im
    from backend.strategy.metrics import compute_group_performance

    def fake_eval(job, price_df_full, params, progress_cb=None):
        symbols, source, _ = job
        assert params["try_all_dependents"] is False and source == "correlation"
        perf = compute_group_performance([{"pnl": 1.0}])
        return {"symbols": list(symbols), "dependent_symbol": symbols[0], "source": source, "sector": None,
                "backtest_metrics": perf.as_dict(), "score": float(len(symbols)), "oos_metrics": None,
                "oos_score": None}

    monkeypatch.setattr(im, "_evaluate_job", fake_eval)

    class InlinePool:  # run the jobs in-process so the patched _evaluate_job is used
        def __init__(self, max_workers, initializer, initargs):
            initializer(*initargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, job):
            from concurrent.futures import Future
            f = Future()
            f.set_result(fn(job))
            return f

    monkeypatch.setattr(im, "ProcessPoolExecutor", InlinePool)
    kept = im.correlation_init(_corr_world(), 2, 3, keep_top_n=10, backbone_params={"window_size": 100},
                               min_r2=0.5, top_hedges=2)
    assert kept and all(g["dependent_symbol"] == g["symbols"][0] for g in kept)
    assert all(g["corr_r2"] is not None and g["corr_r2"] >= 0.5 for g in kept)
    assert all("D" not in g["symbols"] for g in kept)
