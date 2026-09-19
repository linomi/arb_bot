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
from statsmodels.tsa.stattools import adfuller, kpss


@dataclass
class StationarityResult:
    adf_stat: float
    adf_pvalue: float
    kpss_stat: float
    kpss_pvalue: float
    passed: bool


def test_stationarity(residual: np.ndarray, adf_alpha: float, kpss_alpha: float) -> StationarityResult:
    if len(residual) < 10:
        # Not enough data to test meaningfully -- fail closed.
        return StationarityResult(float("nan"), 1.0, float("nan"), 0.0, False)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # statsmodels warns on interpolated p-values near table bounds
        adf_stat, adf_p, *_ = adfuller(residual, autolag="AIC")
        try:
            kpss_stat, kpss_p, *_ = kpss(residual, regression="c", nlags="auto")
        except Exception:
            kpss_stat, kpss_p = float("nan"), 0.0

    passed = bool((adf_p <= adf_alpha) and (kpss_p >= kpss_alpha))
    return StationarityResult(
        float(adf_stat), float(adf_p), float(kpss_stat), float(kpss_p), passed,
    )
