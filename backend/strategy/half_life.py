"""AR(1) half-life of the OLS residual (in bars)."""
from __future__ import annotations
import numpy as np


def residual_half_life_bars(residual: np.ndarray) -> float:
    """
    Estimate mean-reversion half-life of a residual series via AR(1):
        r_t = c + phi * r_{t-1} + e
    half-life = -ln(2) / ln(phi) for 0 < phi < 1.
    Returns +inf when the series is not mean-reverting (phi >= 1 or phi <= 0).
    """
    r = np.asarray(residual, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 10:
        return float("inf")
    y = r[1:]
    x = r[:-1]
    # OLS: y = c + phi x
    n = len(y)
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    var_x = float(np.dot(x - x_mean, x - x_mean))
    if var_x <= 0:
        return float("inf")
    phi = float(np.dot(x - x_mean, y - y_mean) / var_x)
    if not (0 < phi < 1):
        return float("inf")
    return float(-np.log(2.0) / np.log(phi))


def half_life_ok(
    residual: np.ndarray,
    window_size: int,
    *,
    min_bars: float = 2.0,
    max_fraction: float = 1.0 / 3.0,
) -> tuple[bool, float]:
    hl = residual_half_life_bars(residual)
    max_bars = float(window_size) * float(max_fraction)
    ok = (hl >= float(min_bars)) and (hl <= max_bars)
    return ok, hl
