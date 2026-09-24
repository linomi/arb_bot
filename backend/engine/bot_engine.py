"""Background trading loop — public API re-exported from implementation."""
from backend.engine.bot_engine_impl import BotEngine, bot_engine, PartialLegsError  # noqa: F401

__all__ = ["BotEngine", "bot_engine", "PartialLegsError"]
