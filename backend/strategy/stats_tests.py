"""
Stationarity screen applied to the OLS residual (backbone step 3):
  - ADF (Augmented Dickey-Fuller): null hypothesis = unit root (non-stationary).
    We want to REJECT the null -> p-value <= adf_alpha.
  - KPSS: null hypothesis = stationary.
    We want to FAIL TO REJECT the null -> p-value >= kpss_alpha.
A group "passes" only when both agree the residual is stationary.
"""
from dataclasses import dataclass
import warnings
import numpy as np
from statsmodels.tsa.stattools import adfuller, kpss, coint


@dataclass
class StationarityResult:
    adf_stat: float
    adf_pvalue: float
    kpss_stat: float
    kpss_pvalue: float
    passed: bool


# statsmodels' coint() has MacKinnon tables for at most 5 regressors; with 6+ it
# raises IndexError (which used to make every group of 7+ symbols fail closed
# forever). For larger groups we use a Monte-Carlo null instead.
_COINT_MAX_REGRESSORS = 5
_MC_REPS = 1500
_MC_CACHE: dict[tuple[int, int], np.ndarray] = {}


def _df_stat(e: np.ndarray) -> float:
    """Dickey-Fuller t-stat (1 lagged difference, no constant) on a residual series."""
    e = np.asarray(e, dtype=float)
    de = np.diff(e)
    y = de[1:]
    X = np.column_stack([e[1:-1], de[:-1]])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    dof = max(1, len(y) - X.shape[1])
    s2 = float(resid @ resid) / dof
    xtx_inv = np.linalg.pinv(X.T @ X)
    se = float(np.sqrt(max(s2 * xtx_inv[0, 0], 1e-300)))
    return float(coef[0] / se)


def _mc_null(k: int, n: int) -> np.ndarray:
    key = (int(k), int(n))
    if key not in _MC_CACHE:
        rng = np.random.default_rng(12345 + 31 * k + n)
        out = np.empty(_MC_REPS)
        for r in range(_MC_REPS):
            walks = np.cumsum(rng.standard_normal((n, k + 1)), axis=0)
            y, X = walks[:, 0], walks[:, 1:]
            D = np.column_stack([np.ones(n), X])
            coef, *_ = np.linalg.lstsq(D, y, rcond=None)
            out[r] = _df_stat(y - D @ coef)
        _MC_CACHE[key] = np.sort(out)
    return _MC_CACHE[key]


def engle_granger_mc_pvalue(y: np.ndarray, x_cols: list[np.ndarray]) -> tuple[float, float]:
    """Engle-Granger p-value from a simulated null (independent random walks, same
    n and number of regressors). Used when statsmodels has no tables (k > 5)."""
    y = np.asarray(y, dtype=float)
    X = np.column_stack([np.asarray(c, dtype=float) for c in x_cols])
    D = np.column_stack([np.ones(len(y)), X])
    coef, *_ = np.linalg.lstsq(D, y, rcond=None)
    stat = _df_stat(y - D @ coef)
    null = _mc_null(X.shape[1], len(y))
    p = (int(np.searchsorted(null, stat, side="right")) + 1) / (len(null) + 1)
    return float(stat), float(p)


def engle_granger_pvalue(y: np.ndarray, x_cols: list[np.ndarray]) -> tuple[float, float]:
    """
    Engle-Granger cointegration test with MacKinnon critical values.

    Running plain ADF on a *fitted* OLS residual is invalid: the regression
    already minimised the residual variance, so ADF's Dickey-Fuller table is far
    too lenient (simulation on independent random walks: ~14-45% false passes at
    a nominal 5% depending on the number of regressors). statsmodels' `coint`
    uses the correct tables, which depend on the number of regressors.
    """
    if len(x_cols) > _COINT_MAX_REGRESSORS:
        return engle_granger_mc_pvalue(y, x_cols)
    X = np.column_stack([np.asarray(c, dtype=float) for c in x_cols])
    stat, p, _crit = coint(np.asarray(y, dtype=float), X, trend="c", autolag="aic")
    return float(stat), float(p)


def test_stationarity(
    residual: np.ndarray,
    adf_alpha: float,
    kpss_alpha: float,
    *,
    y: np.ndarray | None = None,
    x_cols: list[np.ndarray] | None = None,
    method: str = "engle_granger",
) -> StationarityResult:
    """
    method="engle_granger" (default, needs y and x_cols): EG p-value replaces the
    ADF p-value (still reported in the adf_* fields so the DB/UI are unchanged).
    method="adf_kpss": legacy behaviour (ADF on the residual).
    KPSS is kept as an additional filter in both modes.
    """
    if len(residual) < 10:
        # Not enough data to test meaningfully -- fail closed.
        return StationarityResult(float("nan"), 1.0, float("nan"), 0.0, False)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # statsmodels warns on interpolated p-values near table bounds
        if method == "engle_granger" and y is not None and x_cols:
            try:
                adf_stat, adf_p = engle_granger_pvalue(y, x_cols)
            except Exception:
                adf_stat, adf_p = float("nan"), 1.0  # fail closed
        else:
            adf_stat, adf_p, *_ = adfuller(residual, autolag="AIC")
        try:
            kpss_stat, kpss_p, *_ = kpss(residual, regression="c", nlags="auto")
        except Exception:
            kpss_stat, kpss_p = float("nan"), 0.0

    passed = bool((adf_p <= adf_alpha) and (kpss_p >= kpss_alpha))
    return StationarityResult(
        float(adf_stat), float(adf_p), float(kpss_stat), float(kpss_p), passed,
    )


test_stationarity.__test__ = False  # not a pytest test, despite the name
