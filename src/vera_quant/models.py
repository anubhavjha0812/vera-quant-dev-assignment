"""Typed domain model: instruments, bars/ticks, orders, fills, positions.

Money fields are always `Decimal` — never `float` (CLAUDE.md rule 1).
Rounding to tick size happens only via `vera_quant.money.round_to_tick`;
nothing here re-implements that policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Instrument:
    """A tradeable contract. Built by the contract-master loader (step 4)."""

    exchange: str  # e.g. "NFO", "MCX", "NSE"
    trading_symbol: str
    symbol_token: str
    tick_size: Decimal
    lot_size: int
    # MCX Gold is quoted per 10g but traded in 1kg lots, so this is 100 for
    # that contract (1 quote-unit on the price = 100x that on P&L). 1 for
    # instruments where the quoted unit and traded unit coincide.
    quotation_multiplier: Decimal = Decimal(1)
    expiry: date | None = None


@dataclass(frozen=True)
class Bar:
    instrument_token: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Tick:
    instrument_token: str
    timestamp: datetime
    last_price: Decimal
    last_quantity: int = 0


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants to happen — not yet a broker order.

    Produced by a strategy, consumed by the risk layer (step 9).
    """

    instrument: Instrument
    transaction_type: TransactionType
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    reason: str = ""


@dataclass
class Order:
    """A broker-facing order, tracked through its lifecycle (step 10)."""

    idempotency_key: str
    instrument: Instrument
    transaction_type: TransactionType
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None = None
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class Fill:
    order_idempotency_key: str
    instrument: Instrument
    transaction_type: TransactionType
    quantity: int
    fill_price: Decimal
    fees: Decimal
    filled_at: datetime


@dataclass
class Position:
    """Derived only from fills — never from intents or broker state alone
    (dev-plan rule: "Positions and P&L derived only from fills"). The
    update logic itself lands in step 10; this is the data shape.
    """

    instrument: Instrument
    net_quantity: int = 0
    avg_price: Decimal = field(default_factory=lambda: Decimal(0))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal(0))
