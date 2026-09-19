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
    symbols = Column(JSON, nullable=False)          # list[str], e.g. ["BTCIRT","ETHIRT"]
    dependent_symbol = Column(String, nullable=False)  # the "y" in the OLS
    source = Column(String, nullable=False, default="random")  # random | sector | manual
    sector = Column(String, nullable=True)
    status = Column(String, nullable=False, default="candidate")  # candidate|active|inactive|archived
    params_snapshot = Column(JSON, nullable=True)    # backbone params used when this group was created
    backtest_metrics = Column(JSON, nullable=True)   # metrics computed at init-time backtest
    created_at = Column(DateTime, default=now)

    fits = relationship("OLSFit", back_populates="group", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="group", cascade="all, delete-orphan")


class OLSFit(Base):
    """
    One row per OLS re-fit attempt (whether or not it passed ADF/KPSS, and
    whether or not it led to a trade). This is what step 2-4 of the backbone
    produce each cycle, and what trade markers point back to for the
    "click a mark -> show the OLS params at that time" UI feature.
    """
    __tablename__ = "ols_fits"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    fitted_at = Column(DateTime, default=now, index=True)
    window_start = Column(DateTime, nullable=True)
    window_end = Column(DateTime, nullable=True)
    betas = Column(JSON, nullable=False)      # {"ETHIRT": 1.23, "SOLIRT": 0.44, ...}
    intercept = Column(Float, nullable=False, default=0.0)
    resid_mean = Column(Float, nullable=False)
    resid_std = Column(Float, nullable=False)
    adf_stat = Column(Float, nullable=True)
    adf_pvalue = Column(Float, nullable=True)
    kpss_stat = Column(Float, nullable=True)
    kpss_pvalue = Column(Float, nullable=True)
    passed = Column(Boolean, default=False)
    residual_series = Column(JSON, nullable=True)  # [[timestamp, value], ...] for the fit window, for plotting

    group = relationship("Group", back_populates="fits")


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    ols_fit_id = Column(Integer, ForeignKey("ols_fits.id"), nullable=False)

    direction = Column(String, nullable=False)  # "long_residual" | "short_residual"
    mode = Column(String, nullable=False, default="paper")  # paper | live

    entry_time = Column(DateTime, default=now, index=True)
    entry_z = Column(Float, nullable=False)
    entry_residual = Column(Float, nullable=False)
    entry_prices = Column(JSON, nullable=False)   # {symbol: price}

    status = Column(String, nullable=False, default="open")  # open | closed
    close_time = Column(DateTime, nullable=True)
    close_reason = Column(String, nullable=True)  # close | stop_loss | manual
    close_z = Column(Float, nullable=True)
    close_residual = Column(Float, nullable=True)
    close_prices = Column(JSON, nullable=True)

    pnl = Column(Float, nullable=True)
    fee_paid = Column(Float, nullable=True)

    group = relationship("Group", back_populates="trades")
    ols_fit = relationship("OLSFit")


class Credential(Base):
    """
    Encrypted exchange credentials for live trading. Values are Fernet
    ciphertext, never plaintext, at rest. See backend/security.py.
    """
    __tablename__ = "credentials"
    id = Column(Integer, primary_key=True)
    exchange = Column(String, nullable=False, default="nobitex")
    auth_method = Column(String, nullable=False, default="token")  # token | key_signature
    encrypted_token = Column(Text, nullable=True)
    encrypted_api_key = Column(Text, nullable=True)
    encrypted_api_secret = Column(Text, nullable=True)  # ed25519 private key (PEM), encrypted
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class BotState(Base):
    __tablename__ = "bot_state"
    id = Column(Integer, primary_key=True)
    is_running = Column(Boolean, default=False)
    trading_mode = Column(String, default="paper")  # paper | live
    updated_at = Column(DateTime, default=now, onupdate=now)
