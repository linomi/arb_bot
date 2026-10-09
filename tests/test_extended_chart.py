import asyncio
import datetime as dt
import time

import numpy as np
import pandas as pd

from backend.exchange.xt import XTClient
from backend.routers import groups_router as gr


def test_frozen_residual_applies_same_betas_to_every_bar():
    idx = pd.date_range("2026-01-01", periods=6, freq="min")
    df = pd.DataFrame({"Y": [10., 12, 14, 16, 18, 20], "X": [1., 2, 3, 4, 5, 6]}, index=idx)
    out = gr._frozen_residual_series(df, "Y", {"X": 2.0}, 5.0)
    vals = [v for _, v in out]
    assert vals == [3.0, 3.0, 3.0, 3.0, 3.0, 3.0]  # y - (5 + 2x)
    assert out[0][0] == idx[0].isoformat()


def test_xt_ohlc_paging_over_500_bars():
    tf_ms = 60_000
    now = int(time.time() * 1000)
    start = now - 1200 * tf_ms
    all_rows = [[start + i * tf_ms, 1, 1, 1, 1 + i, 1] for i in range(1200)]

    async def fetch(sym, timeframe=None, since=None, limit=None):
        rows = [r for r in all_rows if r[0] >= since][:limit]
        return rows

    class Ex:
        fetch_ohlcv = staticmethod(fetch)

    c = XTClient.__new__(XTClient)
    c._ex = Ex()
    got = asyncio.run(c._fetch_ohlcv_paged("BTC/USDT:USDT", "1m", 60, start, 1200))
    assert len(got) == 1200 and got[0][0] == start and got[-1][4] == 1200
    assert [r[0] for r in got] == sorted({r[0] for r in got})


def test_xt_ohlc_small_request_is_single_call():
    calls = []

    async def fetch(sym, timeframe=None, since=None, limit=None):
        calls.append(limit)
        return [[1, 1, 1, 1, 1, 1]]

    class Ex:
        fetch_ohlcv = staticmethod(fetch)

    c = XTClient.__new__(XTClient)
    c._ex = Ex()
    asyncio.run(c._fetch_ohlcv_paged("X", "1m", 60, 0, 100))
    assert calls == [100]


def test_chart_history_bars_validated():
    import pytest
    from backend import config_service
    base = {"z_entry": 2.0, "z_close": 0.5, "z_stop_loss": 3.5}
    with pytest.raises(ValueError):
        config_service._validate_backbone({**base, "chart_history_bars": 9000})


def test_older_prices_endpoint_returns_only_bars_before_cutoff(monkeypatch):
    from types import SimpleNamespace

    base = 1_800_000_000

    class MD:
        async def get_ohlc(self, sym, res, bars, end_ts=None):
            end = end_ts or base
            ts = [end - (bars - i) * 60 for i in range(bars + 1)]  # includes one bar at/after the cutoff
            return {"t": ts, "c": [float(i) for i in range(len(ts))]}

        async def aclose(self):
            pass

    monkeypatch.setattr(gr.factory, "build_market_data_client", lambda db: MD())
    monkeypatch.setattr(gr.config_service, "get_section", lambda db, s: {"sampling_time": 60})
    db = SimpleNamespace(get=lambda model, gid: SimpleNamespace(symbols=["A", "B"]))
    out = asyncio.run(gr.older_prices(1, before=base, bars=50, db=db))
    assert out["t"] and max(out["t"]) < base and set(out["c"]) == {"A", "B"}
    assert out["exhausted"] is False and len(out["t"]) == len(out["c"]["A"])


def test_fetch_price_df_forwards_end_ts_only_when_given():
    from backend import utils
    seen = []

    class C:
        async def get_ohlc(self, sym, res, bars, **kw):
            seen.append(kw)
            return {"t": [60, 120], "c": [1.0, 2.0]}

    asyncio.run(utils.fetch_price_df(C(), ["A"], "1", 2))
    asyncio.run(utils.fetch_price_df(C(), ["A"], "1", 2, end_ts=500))
    assert seen == [{}, {"end_ts": 500}]
