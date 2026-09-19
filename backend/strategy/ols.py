"""
OLS step of the backbone: dependent symbol (y) regressed on the other
symbols in the group (x_1..x_n) over the past window. Residual = the
mean-reverting spread the strategy trades.

    y_t = intercept + beta_1 * x1_t + ... + beta_n * xn_t + resid_t
"""
from dataclasses import dataclass
import numpy as np


@dataclass
class OLSResult:
    dependent: str
    independents: list[str]
    betas: dict[str, float]
    intercept: float
    residual: np.ndarray       # residual series over the fit window
    resid_mean: float
    resid_std: float
    fitted_values: np.ndarray


def fit_ols(dependent_symbol: str, price_matrix: dict[str, np.ndarray]) -> OLSResult:
    """
    price_matrix: {symbol: np.ndarray of prices, all same length, aligned in time}
    Uses log-prices for the regression (standard for stat-arb spreads --
    keeps betas scale-free across assets with very different price levels).
    """
    independents = [s for s in price_matrix.keys() if s != dependent_symbol]
    if not independents:
        raise ValueError("Group needs at least 2 symbols (1 dependent + >=1 independent).")

    y = np.log(price_matrix[dependent_symbol])
    X_cols = [np.log(price_matrix[s]) for s in independents]
    X = np.column_stack(X_cols)
    X_design = np.column_stack([np.ones(len(y)), X])  # intercept + betas

    coeffs, *_ = np.linalg.lstsq(X_design, y, rcond=None)
    intercept = float(coeffs[0])
    betas = {sym: float(b) for sym, b in zip(independents, coeffs[1:])}

    fitted = X_design @ coeffs
    residual = y - fitted

    return OLSResult(
        dependent=dependent_symbol,
        independents=independents,
        betas=betas,
        intercept=intercept,
        residual=residual,
        resid_mean=float(np.mean(residual)),
        resid_std=float(np.std(residual, ddof=1)) if len(residual) > 1 else 0.0,
        fitted_values=fitted,
    )


def residual_from_frozen_fit(
    dependent_symbol: str,
    latest_prices: dict[str, float],
    betas: dict[str, float],
    intercept: float,
) -> float:
    """
    Compute the current residual using a FROZEN (already-fitted) OLS -- this
    is what's used while a position is open, so the spread definition
    doesn't drift out from under an open trade (per spec: freeze beta/mean/
    std at entry for exit checks).
    """
    y = np.log(latest_prices[dependent_symbol])
    x_term = sum(betas[sym] * np.log(latest_prices[sym]) for sym in betas)
    fitted = intercept + x_term
    return float(y - fitted)
