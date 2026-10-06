import pandas as pd
from backend.utils import align_price_series


def test_ffill_limited_preserves_rows():
    idx = pd.date_range("2026-01-01", periods=5, freq="min")
    a = pd.Series([1, 2, 3, 4, 5], index=idx)
    b = pd.Series([10, None, None, 13, 14], index=idx)
    df = align_price_series({"A": a, "B": b}, max_ffill_bars=2)
    assert len(df) == 5
    assert df.loc[idx[1], "B"] == 10
    assert df.loc[idx[2], "B"] == 10


def test_gap_beyond_limit_dropped():
    idx = pd.date_range("2026-01-01", periods=6, freq="min")
    a = pd.Series([1.0] * 6, index=idx)
    b = pd.Series([10, None, None, None, 14, 15], index=idx)
    df = align_price_series({"A": a, "B": b}, max_ffill_bars=2)
    assert idx[3] not in df.index
    assert len(df) < 6


def test_empty():
    assert align_price_series({}).empty
