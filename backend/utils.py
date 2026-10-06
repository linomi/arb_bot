"""Small shared helpers."""
import pandas as pd

_SUPPORTED_MINUTE_RESOLUTIONS = [1, 5, 15, 30, 60, 180, 240, 360, 720]


def seconds_to_resolution(sampling_seconds: int) -> str:
    """Map an arbitrary sampling_time (seconds) to the nearest resolution
    string the exchange's OHLC endpoint accepts."""
    minutes = max(1, round(sampling_seconds / 60))
    if minutes >= 1440:
        days = max(1, round(minutes / 1440))
        return f"{min(days, 3)}D"
    best = min(_SUPPORTED_MINUTE_RESOLUTIONS, key=lambda r: abs(r - minutes))
    return str(best)


def align_price_series(series: dict[str, pd.Series], max_ffill_bars: int = 2) -> pd.DataFrame:
    """Outer-join symbol series, forward-fill up to max_ffill_bars, then dropna.

    One illiquid symbol with occasional missing bars should not delete the
    entire row for the group (limited ffill before the hard dropna).
    """
    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(series).sort_index()
    if max_ffill_bars and max_ffill_bars > 0:
        df = df.ffill(limit=int(max_ffill_bars))
    return df.dropna(how="any")


async def fetch_price_df(client, symbols: list[str], resolution: str, bars: int) -> pd.DataFrame:
    series = {}
    for sym in symbols:
        ohlc = await client.get_ohlc(sym, resolution, bars)
        if not ohlc.get("t"):
            continue
        idx = pd.to_datetime(ohlc["t"], unit="s")
        series[sym] = pd.Series(ohlc["c"], index=idx)
    return align_price_series(series, max_ffill_bars=2)
