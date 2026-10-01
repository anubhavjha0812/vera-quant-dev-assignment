"""The grid and stop-and-reverse execution engines.

Both engines are plain functions of `(bar, atr, position, state, params)`
returning a list of `OrderIntent` — no I/O, no hidden state, no network
calls, deterministic given the same inputs. The only state they carry
(grid level progress, SAR direction/trailing stop) is an explicit,
caller-owned dataclass passed in and mutated in place, never module-level
or instance-hidden state, so the exact same functions run bar-by-bar in
both backtest and live (CLAUDE.md rule 3). `*Params` are dataclasses so the
Macro Regime Engine (step 15) can override them per regime.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from vera_quant.models import Bar, OrderIntent, OrderType, Position, TransactionType

# -------------------------------------------------------------------- grid


@dataclass
class GridParams:
    """`k_spacing * atr` is the distance between grid levels."""

    k_spacing: Decimal = Decimal("1.0")
    max_units: int = 4
    quantity_per_unit: int = 1


@dataclass
class GridState:
    anchor: Decimal | None = None
    direction: int = 0  # 0 = no cycle yet, 1 = long cycle, -1 = short cycle
    filled_levels: int = 0  # units currently held in this cycle, 0..max_units


def grid_step(
    bar: Bar,
    atr: Decimal,
    position: Position,
    state: GridState,
    params: GridParams,
) -> list[OrderIntent]:
    """ATR breakout grid.

    A cycle starts with `state.anchor` at the price where the engine was
    last flat. Each bar, the number of `k_spacing * atr` levels the close
    has moved from the anchor (truncated toward zero) is the *target*
    level count for this bar. The engine pyramids in — one order per level
    — up to `target_levels` (capped at `max_units`), and gives back one
    unit per level if the close retraces back toward the anchor (so a gap
    that crosses several levels in one bar fires several entries in that
    one `grid_step` call; a retrace fires the matching exits). Once fully
    flat again (`filled_levels == 0`), the next bar starts a new cycle
    anchored at the then-current close.
    """
    intents: list[OrderIntent] = []

    if atr <= 0:
        return intents  # ATR not warmed up yet — nothing to space levels on

    if state.anchor is None:
        state.anchor = bar.close
        return intents

    step = params.k_spacing * atr
    offset_levels = int((bar.close - state.anchor) / step)

    if state.direction == 0:
        if offset_levels >= 1:
            state.direction = 1
        elif offset_levels <= -1:
            state.direction = -1
        else:
            return intents  # still inside the first level band

    in_cycle_direction = (state.direction == 1 and offset_levels > 0) or (
        state.direction == -1 and offset_levels < 0
    )
    target_levels = min(abs(offset_levels), params.max_units) if in_cycle_direction else 0

    entry_side = TransactionType.BUY if state.direction == 1 else TransactionType.SELL
    exit_side = TransactionType.SELL if state.direction == 1 else TransactionType.BUY

    while state.filled_levels < target_levels:
        state.filled_levels += 1
        intents.append(
            OrderIntent(
                instrument=position.instrument,
                transaction_type=entry_side,
                quantity=params.quantity_per_unit,
                order_type=OrderType.MARKET,
                reason=f"grid_entry_level_{state.filled_levels}",
            )
        )

    while state.filled_levels > target_levels:
        intents.append(
            OrderIntent(
                instrument=position.instrument,
                transaction_type=exit_side,
                quantity=params.quantity_per_unit,
                order_type=OrderType.MARKET,
                reason=f"grid_exit_level_{state.filled_levels}",
            )
        )
        state.filled_levels -= 1

    if state.filled_levels == 0:
        state.anchor = bar.close
        state.direction = 0

    return intents


# --------------------------------------------------------------------- SAR


@dataclass
class SarParams:
    """`k_multiplier * atr` is the trailing-stop distance from the extreme
    price reached since the last entry/flip."""

    k_multiplier: Decimal = Decimal("2.0")
    quantity: int = 1


@dataclass
class SarState:
    direction: int = 0  # 0 = flat/not started, 1 = long, -1 = short
    stop_price: Decimal | None = None
    extreme_price: Decimal | None = None  # highest close while long / lowest while short


def sar_step(
    bar: Bar,
    atr: Decimal,
    position: Position,
    state: SarState,
    params: SarParams,
) -> list[OrderIntent]:
    """ATR trailing-stop stop-and-reverse.

    Always in the market once started: the first bar with a warmed-up ATR
    opens an initial long. Every bar after that, the trailing stop ratchets
    toward the extreme price reached since entry/last flip. A close through
    the stop flips the position to the opposite side in the same bar (one
    order sized at `2 * quantity`: closing the old side and opening the new
    one), and the new side's own trailing stop starts from that close.
    """
    intents: list[OrderIntent] = []

    if atr <= 0:
        return intents

    close = bar.close

    if state.direction == 0:
        state.direction = 1
        state.extreme_price = close
        state.stop_price = close - params.k_multiplier * atr
        intents.append(
            OrderIntent(
                instrument=position.instrument,
                transaction_type=TransactionType.BUY,
                quantity=params.quantity,
                order_type=OrderType.MARKET,
                reason="sar_initial_entry",
            )
        )
        return intents

    assert state.extreme_price is not None
    assert state.stop_price is not None

    if state.direction == 1:
        state.extreme_price = max(state.extreme_price, close)
        state.stop_price = state.extreme_price - params.k_multiplier * atr
        if close <= state.stop_price:
            state.direction = -1
            state.extreme_price = close
            state.stop_price = close + params.k_multiplier * atr
            intents.append(
                OrderIntent(
                    instrument=position.instrument,
                    transaction_type=TransactionType.SELL,
                    quantity=params.quantity * 2,
                    order_type=OrderType.MARKET,
                    reason="sar_flip_to_short",
                )
            )
    else:
        state.extreme_price = min(state.extreme_price, close)
        state.stop_price = state.extreme_price + params.k_multiplier * atr
        if close >= state.stop_price:
            state.direction = 1
            state.extreme_price = close
            state.stop_price = close - params.k_multiplier * atr
            intents.append(
                OrderIntent(
                    instrument=position.instrument,
                    transaction_type=TransactionType.BUY,
                    quantity=params.quantity * 2,
                    order_type=OrderType.MARKET,
                    reason="sar_flip_to_long",
                )
            )

    return intents
