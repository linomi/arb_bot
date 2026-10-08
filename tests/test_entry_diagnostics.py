import logging
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient

from backend.strategy.stats_tests import engle_granger_pvalue, engle_granger_mc_pvalue
from backend.engine.bot_engine import BotEngine


def _walks(k, n=200, seed=0):
    r = np.random.default_rng(seed)
    return np.cumsum(r.normal(size=(k + 1, n)), axis=1)


def test_eg_large_group_returns_finite_p():
    w = _walks(8)
    _, p = engle_granger_pvalue(w[0], [w[i] for i in range(1, 9)])
    assert 0.0 < p <= 1.0


def test_eg_mc_false_pass_rate_is_low():
    passes = 0
    for s in range(60):
        w = _walks(7, seed=100 + s)
        if engle_granger_mc_pvalue(w[0], [w[i] for i in range(1, 8)])[1] <= 0.05:
            passes += 1
    assert passes / 60 < 0.2


def test_eg_mc_accepts_cointegration():
    w = _walks(7, seed=5)
    r = np.random.default_rng(1)
    y = sum(w[1:]) * 0.3 + r.normal(scale=0.5, size=w.shape[1])
    assert engle_granger_mc_pvalue(y, [w[i] for i in range(1, 8)])[1] < 0.05


def test_diag_records_and_scan_summary_logs(caplog):
    eng = BotEngine()
    g = SimpleNamespace(id=7, name="G7")
    eng._diag(g, "waiting_z", "x", z=0.8, z_entry=1.5, stationary=True, adf_p=0.01,
              blockers=["|z|=0.8 < entry 1.5"])
    d = eng._group_diag[7]
    assert d["stage"] == "waiting_z" and d["at"].endswith("Z") and d["blockers"]
    with caplog.at_level(logging.INFO, logger="bot_engine"):
        eng._log_scan_summary([g])
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "entry scan: 1 group(s) waiting_z=1" in msg and "nearest: G7" in msg


def test_diagnostics_route():
    from backend.main import app
    from backend.engine.bot_engine import bot_engine
    bot_engine._group_diag[99] = {"group_id": 99, "name": "X", "stage": "cooldown"}
    try:
        r = TestClient(app).get("/api/bot/diagnostics")
        assert r.status_code == 200
        assert any(g["group_id"] == 99 for g in r.json()["groups"])
    finally:
        bot_engine._group_diag.pop(99, None)
