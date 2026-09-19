"""
In-memory progress tracker for the initialization job.
The UI polls GET /api/init/progress while a run is active.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class InitProgress:
    running: bool = False
    phase: str = "idle"          # idle | fetch_symbols | fetch_ohlc | backtest | persist | done | error
    message: str = ""
    current: int = 0
    total: int = 0
    percent: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


_lock = threading.Lock()
_state = InitProgress()


def get_progress() -> dict:
    with _lock:
        return _state.as_dict()


def reset() -> None:
    with _lock:
        global _state
        _state = InitProgress()


def start(message: str = "Starting initialization…") -> bool:
    """Return False if a run is already in progress."""
    global _state
    with _lock:
        if _state.running:
            return False
        _state = InitProgress(running=True, phase="starting", message=message, percent=0.0)
        return True


def update(
    phase: str | None = None,
    message: str | None = None,
    current: int | None = None,
    total: int | None = None,
    percent: float | None = None,
) -> None:
    with _lock:
        if phase is not None:
            _state.phase = phase
        if message is not None:
            _state.message = message
        if current is not None:
            _state.current = current
        if total is not None:
            _state.total = total
        if percent is not None:
            _state.percent = max(0.0, min(100.0, float(percent)))
        elif _state.total > 0 and current is not None:
            _state.percent = max(0.0, min(100.0, 100.0 * _state.current / _state.total))


def finish(result: dict | None = None) -> None:
    with _lock:
        _state.running = False
        _state.phase = "done"
        _state.percent = 100.0
        _state.message = "Initialization complete."
        _state.result = result
        _state.error = None


def fail(error: str) -> None:
    with _lock:
        _state.running = False
        _state.phase = "error"
        _state.message = error
        _state.error = error
