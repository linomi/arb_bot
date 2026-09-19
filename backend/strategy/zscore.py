"""
Z-score bookkeeping for the residual, and the entry/close/stop decision
rules (backbone step 4 + open-position checks in step 1).
"""
from dataclasses import dataclass


def zscore(residual_value: float, mean: float, std: float) -> float:
    if std == 0 or std != std:  # zero or NaN
        return 0.0
    return (residual_value - mean) / std


@dataclass
class EntryDecision:
    should_enter: bool
    direction: str | None  # "long_residual" | "short_residual" | None
    z: float


def decide_entry(residual_value: float, mean: float, std: float, z_entry: float) -> EntryDecision:
    z = zscore(residual_value, mean, std)
    if z >= z_entry:
        # Residual abnormally high -> expect it to fall -> short the residual
        # (short dependent symbol, long the beta-weighted basket of independents).
        return EntryDecision(True, "short_residual", z)
    if z <= -z_entry:
        # Residual abnormally low -> expect it to rise -> long the residual.
        return EntryDecision(True, "long_residual", z)
    return EntryDecision(False, None, z)


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str | None  # "close" | "stop_loss" | None
    z: float


def decide_exit(
    residual_value: float,
    direction: str,
    mean: float,
    std: float,
    z_close: float,
    z_stop_loss: float,
) -> ExitDecision:
    """
    mean/std here are the FROZEN values from entry time, per spec (freeze
    the OLS/mean/std for managing an open position).
    """
    z = zscore(residual_value, mean, std)

    if direction == "short_residual":
        # Entered because z was high (>= +z_entry); take profit as it reverts toward 0,
        # stop out if it keeps diverging upward.
        if z <= z_close:
            return ExitDecision(True, "close", z)
        if z >= z_stop_loss:
            return ExitDecision(True, "stop_loss", z)
    else:  # long_residual
        if z >= -z_close:
            return ExitDecision(True, "close", z)
        if z <= -z_stop_loss:
            return ExitDecision(True, "stop_loss", z)

    return ExitDecision(False, None, z)
