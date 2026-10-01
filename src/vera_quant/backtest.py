"""The backtest harness: data -> strategy -> risk -> broker, one bar at a
time, writing a trade blotter. Plus a walk-forward window runner.

Loop ordering is what makes `PaperBroker.on_bar` safe against lookahead:
each bar first resolves fills queued from the *previous* bar's decision,
then that bar's own decision is made from its close and queued for the
*next* bar. No decision ever uses a price from a bar not yet "closed."

This harness does NOT go through `order_management.place_idempotent` or
its SQLite journal — crash recovery and idempotent resend are a live-
trading concern (steps 12/14): if a backtest crashes, you rerun it, no
real money moved. It DOES reuse `apply_fill`/`rebuild_positions_from_fills`
for position/P&L truth, so backtest and live derive P&L the identical way.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from vera_quant.brokers.base import Broker
from vera_quant.models import Bar, Fill, Instrument, Order, OrderIntent, OrderStatus, Position
from vera_quant.order_management import apply_fill

DecideFn = Callable[[Bar, Position], list[OrderIntent]]
RiskCheckFn = Callable[[OrderIntent, Position, datetime], OrderIntent | None]


@dataclass(frozen=True)
class TradeBlotterEntry:
    timestamp: datetime
    instrument_token: str
    trading_symbol: str
    transaction_type: str
    quantity: int
    fill_price: Decimal
    fees: Decimal
    reason: str


@dataclass
class BacktestResult:
    fills: list[Fill] = field(default_factory=list)
    blotter: list[TradeBlotterEntry] = field(default_factory=list)
    final_position: Position | None = None
    equity_curve: list[Decimal] = field(default_factory=list)  # realized P&L after each bar


def run_backtest(
    bars: list[Bar],
    instrument: Instrument,
    decide: DecideFn,
    risk_check: RiskCheckFn,
    broker: Broker,
) -> BacktestResult:
    position = Position(instrument=instrument)
    result = BacktestResult()
    order_seq = 0

    for bar in bars:
        # 1. Resolve whatever was queued from a PRIOR bar's decision.
        fills = broker.on_bar(bar)
        for fill in fills:
            apply_fill(position, fill)  # fees are subtracted inside apply_fill
            result.fills.append(fill)
            result.blotter.append(
                TradeBlotterEntry(
                    timestamp=bar.timestamp,
                    instrument_token=fill.instrument.symbol_token,
                    trading_symbol=fill.instrument.trading_symbol,
                    transaction_type=fill.transaction_type.value,
                    quantity=fill.quantity,
                    fill_price=fill.fill_price,
                    fees=fill.fees,
                    reason="",
                )
            )

        # 2. Decide using THIS bar's close (now fully known), queue for the next.
        for intent in decide(bar, position):
            checked = risk_check(intent, position, bar.timestamp)
            if checked is None:
                continue
            order_seq += 1
            order = Order(
                idempotency_key=f"{bar.timestamp.isoformat()}:{order_seq}",
                instrument=checked.instrument,
                transaction_type=checked.transaction_type,
                quantity=checked.quantity,
                order_type=checked.order_type,
                limit_price=checked.limit_price,
                status=OrderStatus.PENDING,
                created_at=bar.timestamp,
            )
            broker.submit_order(order)

        result.equity_curve.append(position.realized_pnl)

    result.final_position = position
    return result


# --------------------------------------------------------------- walk-forward


@dataclass(frozen=True)
class WalkForwardWindow:
    in_sample: list[Bar]
    out_of_sample: list[Bar]


def make_walk_forward_windows(
    bars: list[Bar], in_sample_size: int, out_of_sample_size: int
) -> list[WalkForwardWindow]:
    """Rolling windows: `in_sample_size` bars to fit/confirm parameters on
    (current strategies are rule-based, not fitted — nothing here
    auto-tunes them), followed by `out_of_sample_size` bars that are
    actually evaluated. The window slides forward by `out_of_sample_size`
    each step so every bar is used out-of-sample exactly once.
    """
    if in_sample_size <= 0 or out_of_sample_size <= 0:
        raise ValueError("in_sample_size and out_of_sample_size must be positive")

    windows: list[WalkForwardWindow] = []
    i = 0
    while i + in_sample_size + out_of_sample_size <= len(bars):
        windows.append(
            WalkForwardWindow(
                in_sample=bars[i : i + in_sample_size],
                out_of_sample=bars[i + in_sample_size : i + in_sample_size + out_of_sample_size],
            )
        )
        i += out_of_sample_size
    return windows


def run_walk_forward(
    bars: list[Bar],
    instrument: Instrument,
    in_sample_size: int,
    out_of_sample_size: int,
    decide_factory: Callable[[], DecideFn],
    risk_check: RiskCheckFn,
    broker_factory: Callable[[], Broker],
) -> list[BacktestResult]:
    """Runs `run_backtest` on each out-of-sample segment of a rolling
    walk-forward window, with fresh strategy state and a fresh broker per
    window (`decide_factory`/`broker_factory` are called once per window)
    so windows never leak state into each other.
    """
    windows = make_walk_forward_windows(bars, in_sample_size, out_of_sample_size)
    results = []
    for window in windows:
        decide = decide_factory()
        broker = broker_factory()
        results.append(run_backtest(window.out_of_sample, instrument, decide, risk_check, broker))
    return results
