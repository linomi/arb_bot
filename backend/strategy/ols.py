"""
OLS step of the backbone: dependent symbol (y) regressed on the other
symbols in the group (x_1..x_n) over the past window. Residual = the
mean-reverting spread the strategy trades.

Uses *raw prices* (not log):

    y_t = intercept + beta_1 * x1_t + ... + beta_n * xn_t + resid_t

Betas are therefore in "units of y-price per unit of x-price". A long-
residual hedge of qty_y shares of y holds -beta_j * qty_y shares of each
x_j (sign handled in sizing). Cash PnL of that basket ≈ qty_y * Δresid.
"""
from dataclasses import dataclass
import numpy as np


@dataclass
class OLSResult:
    dependent: str
    independents: list[str]
    betas: dict[str, float]
    intercept: float
    residual: np.ndarray       # residual series over the fit window (price units of y)
    resid_mean: float
    resid_std: float
    fitted_values: np.ndarray


def fit_ols(dependent_symbol: str, price_matrix: dict[str, np.ndarray]) -> OLSResult:
    """
    price_matrix: {symbol: np.ndarray of raw prices, all same length, time-aligned}.
    """
    independents = [s for s in price_matrix.keys() if s != dependent_symbol]
    if not independents:
        raise ValueError("Group needs at least 2 symbols (1 dependent + >=1 independent).")

    y = np.asarray(price_matrix[dependent_symbol], dtype=float)
    X_cols = [np.asarray(price_matrix[s], dtype=float) for s in independents]
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
    Current residual under a FROZEN OLS (entry-time betas/intercept).
    Raw-price residual in the same units as the dependent price.
    """
    y = float(latest_prices[dependent_symbol])
    x_term = sum(float(betas[sym]) * float(latest_prices[sym]) for sym in betas)
    return float(y - (intercept + x_term))
