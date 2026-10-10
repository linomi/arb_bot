"""
ORM models. All group/trade/fit history lives here (SQLite by default) --
no CSV files, per spec.
"""
import datetime as dt
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, JSON, Text
)
from sqlalchemy.orm import relationship
from backend.db import Base


def now():
    return dt.datetime.utcnow()


class ConfigBlob(Base):
    """
    Single-row-per-section config store. Sections: 'backbone', 'init', 'system'.
    Stored as JSON so the UI can add/adjust any parameter without a schema
    migration -- no hard-coded limits baked into the DB layer.
    """
    __tablename__ = "config_blob"
    id = Column(Integer, primary_key=True)
    section = Column(String, unique=True, index=True, nullable=False)
    data = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, default=now, onupdate=now)


class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    symbols = Column(JSON, nullable=False)
    dependent_symbol = Column(String, nullable=False)
    source = Column(String, nullable=False, default="random")
    sector = Column(String, nullable=True)
    status = Column(String, nullable=False, default="candidate")
    # Exchange this group trades on. Default "nobitex" keeps existing rows valid.
    exchange = Column(String, nullable=False, default="nobitex")
    params_snapshot = Column(JSON, nullable=True)
    backtest_metrics = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=now)

    fits = relationship("OLSFit", back_populates="group", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="group", cascade="all, delete-orphan")


class OLSFit(Base):
    __tablename__ = "ols_fits"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    fitted_at = Column(DateTime, default=now, index=True)
    window_start = Column(DateTime, nullable=True)
    window_end = Column(DateTime, nullable=True)
    betas = Column(JSON, nullable=False)
    intercept = Column(Float, nullable=False, default=0.0)
    resid_mean = Column(Float, nullable=False)
    resid_std = Column(Float, nullable=False)
    adf_stat = Column(Float, nullable=True)
    adf_pvalue = Column(Float, nullable=True)
    kpss_stat = Column(Float, nullable=True)
    kpss_pvalue = Column(Float, nullable=True)
    passed = Column(Boolean, default=False)
    residual_series = Column(JSON, nullable=True)

    group = relationship("Group", back_populates="fits")


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    ols_fit_id = Column(Integer, ForeignKey("ols_fits.id"), nullable=False)

    direction = Column(String, nullable=False)
    mode = Column(String, nullable=False, default="paper")

    entry_time = Column(DateTime, default=now, index=True)
    entry_z = Column(Float, nullable=False)
    entry_residual = Column(Float, nullable=False)
    entry_prices = Column(JSON, nullable=False)

    status = Column(String, nullable=False, default="open")
    close_time = Column(DateTime, nullable=True)
    close_reason = Column(String, nullable=True)
    close_z = Column(Float, nullable=True)
    close_residual = Column(Float, nullable=True)
    close_prices = Column(JSON, nullable=True)

    # Legacy / model-space: unit-notional residual PnL fraction (simulate_pnl)
    pnl = Column(Float, nullable=True)
    fee_paid = Column(Float, nullable=True)

    # Beta-aware fills + cash PnL
    legs_entry = Column(JSON, nullable=True)
    legs_close = Column(JSON, nullable=True)
    model_pnl = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    realized_fee = Column(Float, nullable=True)
    trade_notional = Column(Float, nullable=True)
    # NULL = legacy row (trade_notional was the dependent leg's notional); 'gross' = total of all legs
    notional_basis = Column(String, nullable=True)

    group = relationship("Group", back_populates="trades")
    ols_fit = relationship("OLSFit")


class Credential(Base):
    __tablename__ = "credentials"
    id = Column(Integer, primary_key=True)
    exchange = Column(String, nullable=False, default="nobitex")
    auth_method = Column(String, nullable=False, default="token")
    encrypted_token = Column(Text, nullable=True)
    encrypted_api_key = Column(Text, nullable=True)
    encrypted_api_secret = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class BotState(Base):
    __tablename__ = "bot_state"
    id = Column(Integer, primary_key=True)
    is_running = Column(Boolean, default=False)
    trading_mode = Column(String, default="paper")
    # Active exchange for live trading (single-active-exchange architecture).
    # "nobitex" | "xt". Default preserves existing behaviour.
    exchange = Column(String, default="nobitex")
    updated_at = Column(DateTime, default=now, onupdate=now)
