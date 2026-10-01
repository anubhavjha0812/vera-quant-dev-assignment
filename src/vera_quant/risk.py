"""The risk layer: pre-trade caps (clip, don't just reject) and kill
switches (block new orders).

**Flatten behavior is a parked decision** (see DECISIONS.md, "Still
pending") — neither the email nor the dev plan specifies which of the 5
kill switches should also auto-flatten open positions versus only block
new orders. `flatten_intents()` exists and is independently tested, but no
kill switch wires it in yet; every switch here only blocks. Wire a switch
to call `flatten_intents()` once that decision is made — nothing else in
this module needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from vera_quant.models import OrderIntent, OrderType, Position, TransactionType


@dataclass
class RiskParams:
    max_position_per_instrument: int
    max_total_position: int
    max_order_size: int
    max_daily_loss: Decimal  # negative, e.g. Decimal("-10000")
    max_drawdown_pct: Decimal  # e.g. Decimal("0.20") for 20%
    reject_storm_max_rejects: int
    reject_storm_window_seconds: int
    stale_data_max_seconds: int


@dataclass
class RiskState:
    manual_kill: bool = False
    daily_pnl: Decimal = field(default_factory=lambda: Decimal(0))
    peak_equity: Decimal = field(default_factory=lambda: Decimal(0))
    current_equity: Decimal = field(default_factory=lambda: Decimal(0))
    recent_rejects: list[datetime] = field(default_factory=list)
    last_data_timestamp: datetime | None = None


def check_kill_switches(state: RiskState, params: RiskParams, now: datetime) -> set[str]:
    """Which of the 5 kill switches are tripped right now. Pure function of
    state + params + the current time — no side effects.
    """
    tripped: set[str] = set()

    if state.manual_kill:
        tripped.add("manual")

    if state.daily_pnl <= params.max_daily_loss:
        tripped.add("max_daily_loss")

    if state.peak_equity > 0:
        drawdown = (state.peak_equity - state.current_equity) / state.peak_equity
        if drawdown >= params.max_drawdown_pct:
            tripped.add("max_drawdown")

    window_start = now.timestamp() - params.reject_storm_window_seconds
    recent = [t for t in state.recent_rejects if t.timestamp() >= window_start]
    if len(recent) >= params.reject_storm_max_rejects:
        tripped.add("reject_storm")

    if state.last_data_timestamp is not None:
        age = (now - state.last_data_timestamp).total_seconds()
        if age > params.stale_data_max_seconds:
            tripped.add("stale_data")

    return tripped


def _apply_caps(
    intent: OrderIntent, position: Position, total_position: int, params: RiskParams
) -> OrderIntent | None:
    quantity = min(intent.quantity, params.max_order_size)

    if intent.transaction_type == TransactionType.BUY:
        signed_position_delta = quantity
        signed_total_delta = quantity
    else:
        signed_position_delta = -quantity
        signed_total_delta = -quantity

    # Only clip for moves that INCREASE exposure; a reducing order is never
    # blocked by a position cap (it can only bring the book closer to flat).
    if abs(position.net_quantity + signed_position_delta) > abs(position.net_quantity):
        room = params.max_position_per_instrument - abs(position.net_quantity)
        quantity = min(quantity, max(room, 0))

    if abs(total_position + signed_total_delta) > abs(total_position):
        room = params.max_total_position - abs(total_position)
        quantity = min(quantity, max(room, 0))

    if quantity <= 0:
        return None
    if quantity == intent.quantity:
        return intent
    return OrderIntent(
        instrument=intent.instrument,
        transaction_type=intent.transaction_type,
        quantity=quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        reason=intent.reason,
    )


def risk_gate(
    intent: OrderIntent,
    position: Position,
    total_position: int,
    state: RiskState,
    params: RiskParams,
    now: datetime,
) -> OrderIntent | None:
    """The single entry point every `OrderIntent` from a strategy passes
    through before it may become an `Order` (step 10). Kill switches are
    checked first (any tripped switch blocks outright); caps are applied
    second, clipping the quantity rather than rejecting when there's still
    some room.

    `now` is required, not optional with a time.now() default — a risk
    check silently skipped because a caller forgot to pass the clock is
    exactly the kind of bug this layer exists to prevent.
    """
    if check_kill_switches(state, params, now):
        return None
    return _apply_caps(intent, position, total_position, params)


def flatten_intents(positions: list[Position], reason: str) -> list[OrderIntent]:
    """Closing `OrderIntent`s for every nonzero position — the opposite
    side, full size. Not called automatically by any kill switch yet; see
    the module docstring.
    """
    intents: list[OrderIntent] = []
    for position in positions:
        if position.net_quantity == 0:
            continue
        side = TransactionType.SELL if position.net_quantity > 0 else TransactionType.BUY
        intents.append(
            OrderIntent(
                instrument=position.instrument,
                transaction_type=side,
                quantity=abs(position.net_quantity),
                order_type=OrderType.MARKET,
                reason=reason,
            )
        )
    return intents
