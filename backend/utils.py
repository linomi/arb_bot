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


async def fetch_price_df(client, symbols: list[str], resolution: str, bars: int) -> pd.DataFrame:
    series = {}
    for sym in symbols:
        ohlc = await client.get_ohlc(sym, resolution, bars)
        if not ohlc.get("t"):
            continue
        idx = pd.to_datetime(ohlc["t"], unit="s")
        series[sym] = pd.Series(ohlc["c"], index=idx)
    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(series).dropna(how="any").sort_index()
    return df
