"""
Database engine + session factory.

SQLite is used by default (single-file DB under ./data). Swap DATABASE_URL
in .env for Postgres/MySQL if you outgrow SQLite -- SQLAlchemy handles both
without code changes here.
"""
import logging
import os
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

log = logging.getLogger("db")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'arb_bot.db'}")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sqlite_add_columns():
    """Idempotent ADD COLUMN for existing SQLite DBs (create_all won't alter)."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    columns = [
        ("trades", "legs_entry", "JSON"),
        ("trades", "legs_close", "JSON"),
        ("trades", "model_pnl", "FLOAT"),
        ("trades", "realized_pnl", "FLOAT"),
        ("trades", "realized_fee", "FLOAT"),
        ("trades", "trade_notional", "FLOAT"),
        ("trades", "notional_basis", "VARCHAR"),
        ("groups", "exchange", "VARCHAR NOT NULL DEFAULT 'nobitex'"),
        ("bot_state", "exchange", "VARCHAR DEFAULT 'nobitex'"),
    ]
    with engine.begin() as conn:
        for table, col, coltype in columns:
            try:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                existing = {r[1] for r in rows}  # name is index 1
                if col in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                log.info("Added column %s.%s", table, col)
            except Exception as e:
                log.debug("migrate %s.%s: %s", table, col, e)


def _repair_group_exchange():
    """Groups made by Initialization used to be saved as exchange='nobitex' even on XT.
    Nobitex groups are IRT-quoted, so a USDT-settled perpetual can only be an XT group."""
    try:
        with engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE groups SET exchange='xt' WHERE exchange='nobitex' "
                "AND dependent_symbol LIKE '%/USDT:USDT'"
            ))
            if res.rowcount:
                log.warning("Repaired exchange=xt on %s group(s) saved as nobitex", res.rowcount)
    except Exception as e:
        log.debug("repair group exchange: %s", e)


def init_db():
    from backend import models  # noqa: F401  (register models on Base)
    Base.metadata.create_all(bind=engine)
    _sqlite_add_columns()
    _repair_group_exchange()
