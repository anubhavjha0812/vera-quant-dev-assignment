"""Order state machine, idempotent placement, the SQLite write-ahead
journal, and deriving positions/P&L from fills only.

Covers the email's "idempotent placement, order-state reconciliation after
restarts, position and P&L truth, crash recovery." Restart *reconciliation*
against a live broker is step 14's job (it needs a real order-book query);
this module provides what that needs — a durable journal that survives a
crash and a deterministic idempotency key so a retry is always a no-op.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from vera_quant.models import (
    Fill,
    Instrument,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    TransactionType,
)

# SmartAPI's ordertag field — the idempotency key is hashed down to fit it.
# The SmartAPI forum reports a 15-character limit (dev plan Appendix A);
# re-confirm against the live docs before step 12 sends a real order.
ORDERTAG_MAX_LENGTH = 15


# -------------------------------------------------------------- idempotency


def make_idempotency_key(intent_id: str, intent: OrderIntent) -> str:
    """A deterministic key: the same `(intent_id, intent)` always hashes to
    the same key, so retrying the same trading decision is recognisable as
    a duplicate. `intent_id` is the caller's stable identifier for *this*
    decision (e.g. a strategy/bar/level id) — a fresh decision needs a
    fresh `intent_id`, not a new key for the same one.
    """
    canonical = "|".join(
        [
            intent_id,
            intent.instrument.exchange,
            intent.instrument.symbol_token,
            intent.transaction_type.value,
            str(intent.quantity),
            intent.order_type.value,
        ]
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest[:ORDERTAG_MAX_LENGTH]


# ----------------------------------------------------------- state machine


class InvalidTransitionError(Exception):
    pass


_VALID_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.OPEN, OrderStatus.COMPLETE, OrderStatus.REJECTED}),
    OrderStatus.OPEN: frozenset({OrderStatus.COMPLETE, OrderStatus.CANCELLED}),
    OrderStatus.COMPLETE: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


def transition(order: Order, new_status: OrderStatus) -> None:
    """Mutates `order.status` if the transition is valid, else raises."""
    if new_status not in _VALID_TRANSITIONS[order.status]:
        raise InvalidTransitionError(f"{order.status} -> {new_status} is not a valid transition")
    order.status = new_status


# ------------------------------------------------------------------ journal


def _instrument_to_json(inst: Instrument) -> str:
    return json.dumps(
        {
            "exchange": inst.exchange,
            "trading_symbol": inst.trading_symbol,
            "symbol_token": inst.symbol_token,
            "tick_size": str(inst.tick_size),
            "lot_size": inst.lot_size,
            "quotation_multiplier": str(inst.quotation_multiplier),
            "expiry": inst.expiry.isoformat() if inst.expiry else None,
        }
    )


def _instrument_from_json(raw: str) -> Instrument:
    d = json.loads(raw)
    return Instrument(
        exchange=d["exchange"],
        trading_symbol=d["trading_symbol"],
        symbol_token=d["symbol_token"],
        tick_size=Decimal(d["tick_size"]),
        lot_size=d["lot_size"],
        quotation_multiplier=Decimal(d["quotation_multiplier"]),
        expiry=date.fromisoformat(d["expiry"]) if d["expiry"] else None,
    )


class Journal:
    """SQLite write-ahead journal. Money fields are stored as TEXT, never
    REAL, so Decimal precision survives (CLAUDE.md rule 1) — same
    convention as the Parquet storage in market_data.py.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                idempotency_key TEXT PRIMARY KEY,
                instrument_json TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                limit_price TEXT,
                status TEXT NOT NULL,
                broker_order_id TEXT,
                created_at TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_idempotency_key TEXT NOT NULL,
                instrument_json TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                fill_price TEXT NOT NULL,
                fees TEXT NOT NULL,
                filled_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def record_order(self, order: Order) -> None:
        """Write-ahead: call this BEFORE sending the order to the broker."""
        self._conn.execute(
            """
            INSERT INTO orders
                (idempotency_key, instrument_json, transaction_type, quantity,
                 order_type, limit_price, status, broker_order_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.idempotency_key,
                _instrument_to_json(order.instrument),
                order.transaction_type.value,
                order.quantity,
                order.order_type.value,
                str(order.limit_price) if order.limit_price is not None else None,
                order.status.value,
                order.broker_order_id,
                order.created_at.isoformat() if order.created_at else None,
            ),
        )
        self._conn.commit()

    def update_order(self, order: Order) -> None:
        self._conn.execute(
            """
            UPDATE orders
            SET status = ?, broker_order_id = ?
            WHERE idempotency_key = ?
            """,
            (order.status.value, order.broker_order_id, order.idempotency_key),
        )
        self._conn.commit()

    def get_order(self, idempotency_key: str) -> Order | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_order(row)

    def all_orders(self) -> list[Order]:
        rows = self._conn.execute("SELECT * FROM orders").fetchall()
        return [self._row_to_order(row) for row in rows]

    def _row_to_order(self, row: tuple[object, ...]) -> Order:
        (
            idempotency_key,
            instrument_json,
            transaction_type,
            quantity,
            order_type,
            limit_price,
            status,
            broker_order_id,
            created_at,
        ) = row
        assert isinstance(idempotency_key, str)
        assert isinstance(instrument_json, str)
        assert isinstance(transaction_type, str)
        assert isinstance(quantity, int)
        assert isinstance(order_type, str)
        assert isinstance(status, str)
        return Order(
            idempotency_key=idempotency_key,
            instrument=_instrument_from_json(instrument_json),
            transaction_type=TransactionType(transaction_type),
            quantity=quantity,
            order_type=OrderType(order_type),
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
            status=OrderStatus(status),
            broker_order_id=str(broker_order_id) if broker_order_id is not None else None,
            created_at=datetime.fromisoformat(str(created_at)) if created_at else None,
        )

    def record_fill(self, fill: Fill) -> None:
        self._conn.execute(
            """
            INSERT INTO fills
                (order_idempotency_key, instrument_json, transaction_type,
                 quantity, fill_price, fees, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.order_idempotency_key,
                _instrument_to_json(fill.instrument),
                fill.transaction_type.value,
                fill.quantity,
                str(fill.fill_price),
                str(fill.fees),
                fill.filled_at.isoformat(),
            ),
        )
        self._conn.commit()

    def all_fills(self) -> list[Fill]:
        rows = self._conn.execute(
            "SELECT order_idempotency_key, instrument_json, transaction_type, "
            "quantity, fill_price, fees, filled_at FROM fills ORDER BY id"
        ).fetchall()
        fills = []
        for key, instrument_json, transaction_type, quantity, fill_price, fees, filled_at in rows:
            fills.append(
                Fill(
                    order_idempotency_key=str(key),
                    instrument=_instrument_from_json(str(instrument_json)),
                    transaction_type=TransactionType(str(transaction_type)),
                    quantity=int(quantity),
                    fill_price=Decimal(str(fill_price)),
                    fees=Decimal(str(fees)),
                    filled_at=datetime.fromisoformat(str(filled_at)),
                )
            )
        return fills

    def close(self) -> None:
        self._conn.close()


# ------------------------------------------------------- idempotent placement


def place_idempotent(
    intent_id: str,
    intent: OrderIntent,
    journal: Journal,
    send: Callable[[Order], tuple[str | None, OrderStatus]],
) -> Order:
    """The single entry point for placing an order.

    1. Compute the idempotency key from `(intent_id, intent)`.
    2. If an order with that key is already journaled, return it
       unconditionally — `send` is never called again for the same
       decision (duplicate submissions are no-ops).
    3. Otherwise journal a PENDING order (write-ahead, so a crash right
       after this point still leaves a durable record), THEN call `send`.
       If `send` raises, the exception propagates (crash) but the PENDING
       record survives on disk for the next run to find.
    """
    key = make_idempotency_key(intent_id, intent)
    existing = journal.get_order(key)
    if existing is not None:
        return existing

    order = Order(
        idempotency_key=key,
        instrument=intent.instrument,
        transaction_type=intent.transaction_type,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        status=OrderStatus.PENDING,
        created_at=datetime.now(),
    )
    journal.record_order(order)

    broker_order_id, resulting_status = send(order)
    order.broker_order_id = broker_order_id
    transition(order, resulting_status)
    journal.update_order(order)
    return order


# --------------------------------------------------------- position / P&L


def apply_fill(position: Position, fill: Fill) -> None:
    """Updates `position` in place from a single fill. Positions and P&L
    are derived ONLY from fills — never from intents or broker-reported
    state directly (dev-plan rule, step 10).

    `fill.fees` (step 5's cost model) is subtracted from `realized_pnl`
    here, in the one place that touches it — every caller
    (`backtest.run_backtest`, `live_runner.run_live_session`,
    `rebuild_positions_from_fills` below) gets fee-aware P&L for free.
    This used to be duplicated in both runners and silently missing from
    `rebuild_positions_from_fills`, invisible only because fees defaulted
    to zero; real rates (`DECISIONS.md` #13) exposed the drift between a
    live run's P&L and the same P&L rebuilt from the journal after a
    restart — exactly the mismatch step 16's alerting exists to catch.
    """
    signed_qty = fill.quantity if fill.transaction_type == TransactionType.BUY else -fill.quantity
    new_net = position.net_quantity + signed_qty

    adding_or_opening = position.net_quantity == 0 or (
        (signed_qty > 0) == (position.net_quantity > 0)
    )

    if adding_or_opening:
        total_cost = position.avg_price * abs(position.net_quantity) + fill.fill_price * abs(
            signed_qty
        )
        position.avg_price = total_cost / abs(new_net) if new_net != 0 else Decimal(0)
    else:
        closing_qty = min(abs(signed_qty), abs(position.net_quantity))
        direction = 1 if position.net_quantity > 0 else -1
        position.realized_pnl += direction * (fill.fill_price - position.avg_price) * closing_qty
        if abs(signed_qty) > abs(position.net_quantity):
            # Flipped through flat: the remainder opens a new position at
            # the fill price.
            position.avg_price = fill.fill_price
        # else: a partial reduction leaves the remaining position's
        # avg_price unchanged.

    position.net_quantity = new_net
    position.realized_pnl -= fill.fees


def rebuild_positions_from_fills(fills: list[Fill]) -> dict[str, Position]:
    """Replays every fill, in order, to rebuild each instrument's
    position/P&L from nothing — what a restart does with `journal.all_fills()`.
    """
    positions: dict[str, Position] = {}
    for fill in sorted(fills, key=lambda f: f.filled_at):
        key = fill.instrument.symbol_token
        if key not in positions:
            positions[key] = Position(instrument=fill.instrument)
        apply_fill(positions[key], fill)
    return positions
