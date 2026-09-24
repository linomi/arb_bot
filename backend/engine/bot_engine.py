"""
Background trading loop. Raw-price OLS residual; beta share ratios.

Paper: cash PnL from residual model (qty_y * ΔR − fees).
Live: PnL from Nobitex position.PNL after close (account data).

Safety (audit-driven):
  - Pre-trade margin balance check (live) — refuse if balance unreadable
  - Scale trade_notional down to fit free balance (keep ≥50k IRT/leg)
  - Sequential legs with rollback of filled legs on partial failure
  - Close fallback only if position still has liability
  - Exit management runs even when bot is "stopped" (no new entries only)
  - clientOrderId for idempotency; opened_after for position resolution
  - Store filled_qty when available
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import traceback
import uuid

from sqlalchemy.orm.exc import ObjectDeletedError

from backend.db import SessionLocal
from backend import config_service
from backend.models import Group, Trade, OLSFit, BotState
from backend.bot_state_service import get_or_create_bot_state
from backend.exchange import factory
from backend.exchange.paper import PaperExchangeClient
from backend.exchange.nobitex import NobitexClient, NobitexError
from backend.utils import seconds_to_resolution, fetch_price_df
from backend.strategy.ols import fit_ols, residual_from_frozen_fit
from backend.strategy.stats_tests import test_stationarity
from backend.strategy.zscore import decide_entry, decide_exit
from backend.strategy.sizing import (
    leg_orders,
    close_legs_from_entry,
    MIN_ORDER_VALUE_IRT,
    total_required_collateral,
    assert_same_quote,
)
from backend.strategy.pnl import residual_cash_pnl, legs_gross_notional, entry_target_check

log = logging.getLogger("bot_engine")

# Refuse entry if required collateral exceeds this fraction of free balance.
BALANCE_SAFETY_FRACTION = 0.85
# Extra headroom for fees / mark-price drift between sizing and fill.
BALANCE_BUFFER_IRT = 20_000.0
