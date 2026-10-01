"""The live Angel One SmartAPI broker adapter. Its own package — no
import of `vera_quant.brokers.paper` anywhere in it; deleting that
package leaves this one fully working (CLAUDE.md rule 5).

Safety rules this module enforces, independent of whatever picked this
broker in the first place (step 14's factory):
- `placeOrder` is only ever reached when `broker_mode == "live"` — a
  defence-in-depth check, not the only gate (CLAUDE.md rule 10).
- A hard `LiveSafetyLimits` ceiling (max order size, max orders/day) on
  top of whatever the strategy/risk layer already allowed, so a bug
  upstream can't send an unbounded number or size of real orders.
- A 403 means the rate limit was hit, not an expired session: back off,
  never re-login on it. Re-login happens only on a genuine
  `SessionExpiredError`.
- A timeout is never blindly retried as a resend — the order book is
  checked by `ordertag` (the idempotency key) first; if it's already
  there, that result is returned instead of placing a duplicate.
- A pre-trade margin check (when `required_margin_per_order` is set)
  blocks the order if available margin is insufficient.

Exact SmartAPI SDK method names/response shapes used here are this
adapter's best-effort mapping (see client.py's docstring) — confirm
against the installed SDK before using a real `SmartConnect` instance.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from vera_quant.brokers.live_smartapi.client import (
    RateLimitError,
    SessionExpiredError,
    SmartApiClient,
)
from vera_quant.models import Bar, Fill, Order, OrderStatus, OrderType
from vera_quant.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "STOPLOSS_LIMIT",
    OrderType.SL_M: "STOPLOSS_MARKET",
}


@dataclass
class LiveSafetyLimits:
    """A ceiling independent of strategy sizing — see module docstring."""

    max_order_size: int
    max_daily_orders: int


@dataclass
class SmartApiBroker:
    client: SmartApiClient
    broker_mode: str  # "paper" or "live" — only "live" may ever call placeOrder
    live_limits: LiveSafetyLimits
    rate_limiters: dict[str, RateLimiter]
    sleep: Callable[[float], None] = time.sleep
    retry_delay_seconds: float = 1.0
    max_retries: int = 3
    required_margin_per_order: Decimal | None = None

    _orders_sent_today: int = field(default=0, init=False)
    _first_order_logged: bool = field(default=False, init=False)

    def submit_order(self, order: Order) -> tuple[str | None, OrderStatus]:
        if self.broker_mode != "live":
            return None, OrderStatus.REJECTED

        if order.quantity > self.live_limits.max_order_size:
            logger.error("order quantity exceeds live safety max_order_size, rejecting")
            return None, OrderStatus.REJECTED

        if self._orders_sent_today >= self.live_limits.max_daily_orders:
            logger.error("live safety max_daily_orders reached, rejecting")
            return None, OrderStatus.REJECTED

        if self.required_margin_per_order is not None and not self._has_sufficient_margin():
            logger.error("pre-trade margin check failed, rejecting order")
            return None, OrderStatus.REJECTED

        if not self._first_order_logged:
            logger.info(
                "first live order of session",
                extra={
                    "extra_fields": {
                        "symbol": order.instrument.trading_symbol,
                        "side": order.transaction_type.value,
                        "quantity": order.quantity,
                        "order_type": order.order_type.value,
                        "idempotency_key": order.idempotency_key,
                    }
                },
            )
            self._first_order_logged = True

        broker_order_id, status = self._place_with_retry(order)
        if status in (OrderStatus.OPEN, OrderStatus.COMPLETE):
            self._orders_sent_today += 1
        return broker_order_id, status

    def on_bar(self, bar: Bar) -> list[Fill]:
        # Live fills come from the WebSocket order-update stream (step 13),
        # not from bar-close simulation — nothing to do here yet.
        return []

    # ------------------------------------------------------------- internals

    def _has_sufficient_margin(self) -> bool:
        self.rate_limiters["margin"].acquire(sleep=self.sleep)
        response = self.client.get_margin({})
        data = response.get("data", {})
        assert isinstance(data, dict)
        available = Decimal(str(data.get("availablecash", "0")))
        assert self.required_margin_per_order is not None
        return available >= self.required_margin_per_order

    def _order_to_params(self, order: Order) -> dict[str, str]:
        return {
            "variety": "NORMAL",
            "tradingsymbol": order.instrument.trading_symbol,
            "symboltoken": order.instrument.symbol_token,
            "transactiontype": order.transaction_type.value,
            "exchange": order.instrument.exchange,
            "ordertype": _ORDER_TYPE_MAP[order.order_type],
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(order.limit_price) if order.limit_price is not None else "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(order.quantity),
            "ordertag": order.idempotency_key,
        }

    def _place_with_retry(self, order: Order) -> tuple[str | None, OrderStatus]:
        params = self._order_to_params(order)

        for attempt in range(self.max_retries):
            try:
                self.rate_limiters["place_modify_cancel"].acquire(sleep=self.sleep)
                response = self.client.place_order(params)
            except RateLimitError:
                # 403 -- back off, never re-login.
                self.sleep(self.retry_delay_seconds * (attempt + 1))
                continue
            except SessionExpiredError:
                self._relogin()
                continue
            except TimeoutError:
                existing = self._find_in_order_book(order.idempotency_key)
                if existing is not None:
                    return existing
                self.sleep(self.retry_delay_seconds * (2**attempt))
                continue

            if response.get("status"):
                data = response.get("data", {})
                assert isinstance(data, dict)
                return str(data.get("orderid")), OrderStatus.OPEN

            self.sleep(self.retry_delay_seconds * (2**attempt))

        return None, OrderStatus.REJECTED

    def _relogin(self) -> None:
        # Real credentials (client code, password, TOTP secret) come from
        # config/env in step 14's wiring, not hardcoded here.
        self.client.generate_session("", "", "")

    def _find_in_order_book(self, ordertag: str) -> tuple[str | None, OrderStatus] | None:
        self.rate_limiters["order_book"].acquire(sleep=self.sleep)
        response = self.client.get_order_book()
        rows = response.get("data", [])
        assert isinstance(rows, list)
        for row in rows:
            assert isinstance(row, dict)
            if row.get("ordertag") == ordertag:
                return str(row.get("orderid")), self._status_from_order_book_row(row)
        return None

    @staticmethod
    def _status_from_order_book_row(row: dict[str, str]) -> OrderStatus:
        raw = str(row.get("orderstatus", "")).lower()
        mapping = {
            "open": OrderStatus.OPEN,
            "complete": OrderStatus.COMPLETE,
            "cancelled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
        }
        return mapping.get(raw, OrderStatus.OPEN)
