"""
Signal refinements shared by the live engine and the backtester (both must call these the same way):

- out-of-sample z-score: fit the OLS on the window WITHOUT the current bar, score the current bar against
  it, and scale by the OLS prediction-error std (dof-corrected sigma * sqrt(1 + leverage of the new point)).
  The in-sample residual of the last fitted bar is pulled toward 0 by the fit itself and its std is too
  small, which is why 1-minute stops kept filling at |z| 8-13;
- expected-value gate: a trade must be worth taking once the stop is priced in, not only at a perfect close;
- jump / volatility-regime filter: skip when one of the last bars moved the residual by many times its usual
  one-bar move (one coin hit by news, a listing, funding), or while the residual is far more volatile than in
  the fit window;
- half-life time stop: give a trade a few half-lives to revert, then let it go.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from backend.strategy.half_life import residual_half_life_bars
from backend.strategy.ols import OLSResult, fit_ols


def signal_cfg_from_params(p: dict) -> dict:
    """The backbone settings these refinements read, with their defaults."""
    return {
        "zscore_method": str(p.get("zscore_method", "oos") or "oos"),
        "z_entry_max": float(p.get("z_entry_max", 0) or 0),
        "ev_gate": bool(p.get("ev_gate", True)),
        "ev_win_rate": float(p.get("ev_win_rate", 0.65) or 0),
        "ev_stop_overshoot": float(p.get("ev_stop_overshoot", 0.5) or 0),
        "jump_max_sigma": float(p.get("jump_max_sigma", 4.0) or 0),
        "jump_bars": max(1, int(p.get("jump_bars", 2) or 2)),
        "vol_regime_max": float(p.get("vol_regime_max", 2.0) or 0),
        "vol_regime_bars": max(5, int(p.get("vol_regime_bars", 20) or 20)),
        "max_holding_halflives": float(p.get("max_holding_halflives", 3.0) or 0),
    }


@dataclass
class ScoredFit:
    fit: OLSResult              # OLS on the fit part of the window
    resid_now: float            # residual of the current bar
    mean: float
    std: float                  # z-score scale; frozen into the trade for its exits
    residual: np.ndarray        # fit residuals followed by the current bar's (oos), one per window bar
    y: np.ndarray               # fit part of the window, for the stationarity test
    x_cols: list[np.ndarray]

    @property
    def z(self) -> float:
        return (self.resid_now - self.mean) / self.std if self.std > 0 else 0.0


def window_bars(window_size: int, method: str) -> int:
    """Bars a scored fit needs: the fit window, plus the scored bar when it is out of sample."""
    return int(window_size) + (1 if method == "oos" else 0)


def scored_fit(dependent: str, price_matrix: dict[str, np.ndarray], method: str = "oos") -> ScoredFit:
    """``price_matrix`` holds ``window_bars(W, method)`` bars per symbol; the last one is the current bar.

    "oos": fit on all bars but the last, score the last out of sample.
    "in_sample": the legacy behaviour (fit on all bars, z of the last fitted residual)."""
    others = [s for s in price_matrix if s != dependent]
    if method != "oos":
        fit = fit_ols(dependent, price_matrix)
        return ScoredFit(fit, float(fit.residual[-1]), fit.resid_mean, fit.resid_std, fit.residual,
                         np.asarray(price_matrix[dependent], dtype=float),
                         [np.asarray(price_matrix[s], dtype=float) for s in others])

    if not others:
        raise ValueError("Group needs at least 2 symbols (1 dependent + >=1 independent).")
    fit_part = {s: np.asarray(v, dtype=float)[:-1] for s, v in price_matrix.items()}
    y = fit_part[dependent]
    X = np.column_stack([np.ones(len(y))] + [fit_part[s] for s in others])
    # One SVD gives both the least-squares fit (what np.linalg.lstsq does, same rank cutoff) and the
    # leverage of the scored bar, x0' (X'X)^+ x0 = |diag(1/s) V' x0|^2. Refits run on every backtest bar.
    U, sv, Vt = np.linalg.svd(X, full_matrices=False)
    inv = np.where(sv > np.finfo(float).eps * max(X.shape) * sv[0], 1.0 / sv, 0.0)
    coeffs = Vt.T @ (inv * (U.T @ y))
    fitted = X @ coeffs
    residual = y - fitted
    fit = OLSResult(
        dependent=dependent, independents=others,
        betas={s: float(b) for s, b in zip(others, coeffs[1:])}, intercept=float(coeffs[0]),
        residual=residual, resid_mean=float(np.mean(residual)),
        resid_std=float(np.std(residual, ddof=1)) if len(residual) > 1 else 0.0, fitted_values=fitted,
    )
    x_now = np.array([1.0] + [float(price_matrix[s][-1]) for s in others])
    resid_now = float(price_matrix[dependent][-1]) - float(x_now @ coeffs)

    n, p = X.shape
    sigma = float(np.sqrt(np.dot(residual, residual) / max(1, n - p)))
    lev = float(np.sum((inv * (Vt @ x_now)) ** 2))
    std = sigma * float(np.sqrt(1.0 + lev))
    return ScoredFit(fit, resid_now, fit.resid_mean, std, np.append(residual, resid_now),
                     y, [fit_part[s] for s in others])


def ev_check(
    *, z_now: float, sigma_rel: float, z_close: float, z_stop: float, cost_rate: float,
    win_rate: float, stop_overshoot: float = 0.5,
) -> tuple[bool, dict]:
    """Expected net profit of an entry as a fraction of gross, with the stop priced in:

        gain = sigma_rel * (|z| - z_close) - 2 * cost
        loss = sigma_rel * (z_stop + overshoot - |z|) + 2 * cost
        ev   = p * gain - (1 - p) * loss          (must be > 0)

    ``overshoot`` is how far past the stop level a stop typically fills (cycles are discrete)."""
    az = abs(float(z_now))
    cost = 2.0 * float(cost_rate)
    gain = sigma_rel * (az - z_close) - cost
    loss = sigma_rel * (z_stop + stop_overshoot - az) + cost
    p = min(1.0, max(0.0, float(win_rate)))
    ev = p * gain - (1.0 - p) * loss
    det = {"ev_frac": ev, "gain_frac": gain, "loss_frac": loss, "win_rate": p}
    if ev <= 0:
        det["reject_reason"] = f"expected value {ev * 100:.3f}% of gross <= 0 (win rate {p:.0%})"
        return False, det
    return True, det


def group_win_rate(backtest_metrics: dict | None, default: float, prior_trades: int = 20) -> float:
    """Win rate to assume for a group: its backtest win rate shrunk toward the configured default by
    ``prior_trades`` pseudo-trades, so a handful of (in-sample, selection-biased) backtest trades cannot move
    it far. No backtest = the default."""
    m = backtest_metrics or {}
    try:
        n = int(m.get("trade_count") or 0)
        wr = float(m.get("win_rate") or 0)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return (wr * n + default * prior_trades) / (n + prior_trades)


def regime_block(residual: np.ndarray, cfg: dict) -> str | None:
    """Reason to skip the entry, or None. ``residual`` ends with the current bar."""
    d = np.diff(np.asarray(residual, dtype=float))
    jb = int(cfg.get("jump_bars", 2))
    jmax = float(cfg.get("jump_max_sigma", 0) or 0)
    if jmax > 0 and len(d) > jb + 10:
        base = float(np.std(d[:-jb], ddof=1))
        big = float(np.max(np.abs(d[-jb:])))
        if base > 0 and big / base > jmax:
            return f"jump: a one-bar residual move of {big / base:.1f}x its usual size in the last {jb} bar(s) (max {jmax:g}x)"
    vmax = float(cfg.get("vol_regime_max", 0) or 0)
    vb = int(cfg.get("vol_regime_bars", 20))
    if vmax > 0 and len(d) >= 2 * vb:
        base = float(np.std(d[:-vb], ddof=1))
        recent = float(np.std(d[-vb:], ddof=1))
        if base > 0 and recent / base > vmax:
            return f"volatility regime: last {vb} bars {recent / base:.1f}x the fit window (max {vmax:g}x)"
    return None


def z_cap_block(z: float, cfg: dict) -> str | None:
    cap = float(cfg.get("z_entry_max", 0) or 0)
    if cap > 0 and abs(z) > cap:
        return f"|z|={abs(z):.2f} above z_entry_max {cap:g}"
    return None


def half_life_limit_bars(residual, cfg: dict) -> float | None:
    """Bars a trade may stay open under the half-life time stop, or None (off / not mean-reverting)."""
    mult = float(cfg.get("max_holding_halflives", 0) or 0)
    if mult <= 0 or residual is None or len(residual) < 10:
        return None
    hl = residual_half_life_bars(np.asarray(residual, dtype=float))
    if not np.isfinite(hl):
        return None
    return mult * hl
