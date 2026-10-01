"""The one place the `Broker` interface is defined. Abstract methods only
— no implementation code, no import of either concrete broker. Every
broker (paper here in step 11, live SmartAPI in step 12) implements this
and nothing outside the broker factory (step 14) ever imports a concrete
broker class directly (CLAUDE.md rule 4).
"""
from __future__ import annotations

from typing import Protocol

from vera_quant.models import Bar, Fill, Order, OrderStatus


class Broker(Protocol):
    def submit_order(self, order: Order) -> tuple[str | None, OrderStatus]:
        """Place one order. Returns `(broker_order_id, resulting_status)`
        — this exact shape is also `order_management.place_idempotent`'s
        `send` callback."""
        ...

    def on_bar(self, bar: Bar) -> list[Fill]:
        """Called once per bar in the main loop, after that bar's order
        submissions. Returns any fills that occurred. For a live broker
        this drains WebSocket-reported fills; for the paper broker it runs
        the fill simulation."""
        ...
