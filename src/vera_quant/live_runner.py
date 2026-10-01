"""Assembles the live/paper engine: broker factory, startup reconciliation,
and the journal-backed session loop (vs. backtest.py's lighter loop that
skips the journal — see that module's docstring for why).

The broker is chosen by `BROKER_MODE` through `create_broker()`, which
imports *only* the selected package at runtime — a lazy import inside each
branch, never a top-of-file import of both — so a `paper` run never
touches `brokers.live_smartapi` and a `live` run never touches
`brokers.paper` (CLAUDE.md rule 5, verified at runtime by
`test_live_runner.py::test_create_broker_paper_mode_never_imports_live_smartapi`,
not just by the static source-scan in `test_broker_isolation.py`).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from vera_quant.brokers.base import Broker
from vera_quant.costs import CostRateSchedule
from vera_quant.models import Bar, Order, OrderIntent, OrderStatus, Position
from vera_quant.order_management import (
    Journal,
    apply_fill,
    place_idempotent,
    rebuild_positions_from_fills,
)
from vera_quant.rate_limiter import RateLimiter

DecideFn = Callable[[Bar, Position], list[OrderIntent]]
RiskCheckFn = Callable[[OrderIntent, Position, datetime], OrderIntent | None]

_TERMINAL_OR_RESUMABLE = {OrderStatus.PENDING, OrderStatus.OPEN}


@dataclass
class LiveRunnerConfig:
    broker_mode: str  # "paper" | "live"
    confirm_live: bool = False
    # paper-specific
    paper_slippage_ticks: Decimal = field(default_factory=lambda: Decimal(0))
    paper_cost_schedule: CostRateSchedule = field(default_factory=CostRateSchedule)
    # live-specific
    live_client: object | None = None
    live_safety_limits: object | None = None


def create_broker(config: LiveRunnerConfig) -> Broker:
    """The broker factory — see module docstring for the lazy-import rule."""
    if config.broker_mode == "paper":
        from vera_quant.brokers.paper import PaperBroker

        return PaperBroker(
            slippage_ticks=config.paper_slippage_ticks,
            cost_schedule=config.paper_cost_schedule,
            is_option=False,
        )

    if config.broker_mode == "live":
        if not config.confirm_live:
            raise RuntimeError(
                "BROKER_MODE=live requires explicit run-time confirmation "
                "(--confirm-live) in addition to the config flag — "
                "see CLAUDE.md rule 10."
            )
        from vera_quant.brokers.live_smartapi import LiveSafetyLimits, SmartApiBroker

        assert config.live_client is not None
        assert isinstance(config.live_safety_limits, LiveSafetyLimits)
        return SmartApiBroker(
            client=config.live_client,  # type: ignore[arg-type]
            broker_mode="live",
            live_limits=config.live_safety_limits,
            rate_limiters={
                "place_modify_cancel": RateLimiter(9, 1.0),
                "order_book": RateLimiter(1, 1.0),
                "margin": RateLimiter(10, 1.0),
            },
        )

    raise ValueError(f"unknown BROKER_MODE {config.broker_mode!r}")


# ------------------------------------------------------------- reconciliation


@dataclass
class ReconciliationResult:
    positions: dict[str, Position]
    pending_orders: list[Order]
    mismatches: list[str]


def reconcile_on_startup(
    journal: Journal,
    broker_mode: str,
    live_client: object | None,
    order_book_rate_limiter: RateLimiter | None,
    sleep: Callable[[float], None] = time.sleep,
) -> ReconciliationResult:
    """On startup: rebuild positions from the journal's fills (the only
    source of P&L truth, step 10), find still-pending orders, and — in
    live mode — diff them against the broker's own order book.
    """
    fills = journal.all_fills()
    positions = rebuild_positions_from_fills(fills)

    orders = journal.all_orders()
    pending = [o for o in orders if o.status in _TERMINAL_OR_RESUMABLE]

    mismatches: list[str] = []

    if broker_mode == "live":
        assert live_client is not None and order_book_rate_limiter is not None
        order_book_rate_limiter.acquire(sleep=sleep)
        response = live_client.get_order_book()  # type: ignore[attr-defined]
        rows = response.get("data", [])
        broker_tags = {row["ordertag"] for row in rows}
        journal_tags = {o.idempotency_key for o in pending}

        for tag in journal_tags - broker_tags:
            mismatches.append(
                f"order {tag} is PENDING/OPEN in the journal "
                "but missing from the broker's order book"
            )
        for tag in broker_tags - {o.idempotency_key for o in orders}:
            mismatches.append(f"order {tag} is in the broker's order book but was never journaled")

    return ReconciliationResult(positions=positions, pending_orders=pending, mismatches=mismatches)


def resume_broker_with_pending_orders(broker: Broker, pending_orders: list[Order]) -> None:
    """Re-queues whatever was still PENDING/OPEN in the journal into a
    freshly constructed broker instance, so simulation/live processing can
    continue correctly after a restart.
    """
    for order in pending_orders:
        broker.submit_order(order)


# ------------------------------------------------------------- session loop


def run_live_session(
    bars: list[Bar],
    instrument: object,
    decide: DecideFn,
    risk_check: RiskCheckFn,
    broker: Broker,
    journal: Journal,
) -> Position:
    """Like `backtest.run_backtest`, but every order goes through the
    journal via `place_idempotent` — this is what the real live/paper
    runner uses, so a crash leaves a durable, recoverable record. The
    lighter `backtest.run_backtest` intentionally skips the journal: a
    crashed backtest is just rerun, no real decision was ever at risk.
    """
    from vera_quant.models import Instrument  # local import: typing-only convenience

    assert isinstance(instrument, Instrument)
    position = Position(instrument=instrument)
    intent_seq = 0

    for bar in bars:
        fills = broker.on_bar(bar)
        for fill in fills:
            apply_fill(position, fill)
            position.realized_pnl -= fill.fees
            journal.record_fill(fill)

        for intent in decide(bar, position):
            checked = risk_check(intent, position, bar.timestamp)
            if checked is None:
                continue
            intent_seq += 1
            intent_id = f"{bar.timestamp.isoformat()}:{intent_seq}"

            def _send(order: Order, _broker: Broker = broker) -> tuple[str | None, OrderStatus]:
                return _broker.submit_order(order)

            place_idempotent(intent_id, checked, journal, send=_send)

    return position
