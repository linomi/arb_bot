"""Pydantic models for API I/O. Config sections stay as free-form dicts
(no hard-coded field whitelist) -- validation there is just type-level
(int/float/str/bool via JSON), matching the "no stupid hard limits" spec."""
from typing import Optional, Any
from pydantic import BaseModel


class ConfigSectionUpdate(BaseModel):
    data: dict[str, Any]


class InitRunRequest(BaseModel):
    method: Optional[str] = None  # overrides init.method if provided ("random"|"sector")
    activate: bool = True         # if true, surviving groups get status="active" immediately


class GroupOut(BaseModel):
    id: int
    name: str
    symbols: list[str]
    dependent_symbol: str
    source: str
    sector: Optional[str] = None
    status: str
    backtest_metrics: Optional[dict] = None
    created_at: str

    class Config:
        from_attributes = True


class GroupStatusUpdate(BaseModel):
    status: str  # "active" | "inactive" | "archived"


class TradeOut(BaseModel):
    id: int
    group_id: int
    ols_fit_id: int
    direction: str
    mode: str
    entry_time: str
    entry_z: float
    entry_residual: float
    entry_prices: dict
    status: str
    close_time: Optional[str] = None
    close_reason: Optional[str] = None
    close_z: Optional[float] = None
    close_residual: Optional[float] = None
    close_prices: Optional[dict] = None
    pnl: Optional[float] = None
    fee_paid: Optional[float] = None

    class Config:
        from_attributes = True


class OLSFitOut(BaseModel):
    id: int
    group_id: int
    fitted_at: str
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    betas: dict
    intercept: float
    resid_mean: float
    resid_std: float
    adf_stat: Optional[float] = None
    adf_pvalue: Optional[float] = None
    kpss_stat: Optional[float] = None
    kpss_pvalue: Optional[float] = None
    passed: bool
    residual_series: Optional[list] = None

    class Config:
        from_attributes = True


class BotStateOut(BaseModel):
    is_running: bool
    trading_mode: str
    last_error: Optional[str] = None


class BotModeUpdate(BaseModel):
    trading_mode: str  # "paper" | "live"


class CredentialIn(BaseModel):
    auth_method: str = "token"          # "token" | "key_signature"
    token: Optional[str] = None
    api_key: Optional[str] = None
    api_secret_pem: Optional[str] = None


class CredentialStatusOut(BaseModel):
    configured: bool
    auth_method: Optional[str] = None
    exchange: str = "nobitex"
