"""Tiny in-process event hub. The engine and API call ``emit(kind, **data)``; whatever sinks
are registered (the Telegram bot, tests) receive it. Emitting never raises and is a no-op
when nothing is listening, so the trading code does not depend on notifications."""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("notify")

Sink = Callable[[str, dict], None]


class Hub:
    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def add_sink(self, sink: Sink) -> None:
        if sink not in self._sinks:
            self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def emit(self, kind: str, **data) -> None:
        for sink in list(self._sinks):
            try:
                sink(kind, data)
            except Exception:  # a broken sink must never affect trading
                log.exception("notify sink failed for %s", kind)


hub = Hub()


def emit(kind: str, **data) -> None:
    hub.emit(kind, **data)
