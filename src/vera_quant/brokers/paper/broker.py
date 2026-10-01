"""The paper (simulated) broker. Its own package — no import of
`vera_quant.brokers.live_smartapi` anywhere in it; deleting that package
leaves this one fully working (CLAUDE.md rule 5).

Fill rules (dev plan, step 11):
- market orders fill at the NEXT bar's open (decide at close, execute at
  next open — this one-bar lag is what makes `on_bar` safe to call with
  "today's" bar for orders submitted on a *previous* bar, never the
  current one: see backtest.py's loop for why that ordering holds).
- limit/stop orders fill when a later bar's range crosses them, at that
  bar's open if it gapped straight through.
- if both a stop and a limit for the same instrument trigger in one bar,
  the stop (the adverse side) is resolved first.
- slippage in ticks applies to market and triggered-stop fills (which
  behave like a market order once triggered) — never to a true limit
  fill, which by definition can't execute worse than its limit price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from vera_quant.costs import CostRateSchedule, compute_trade_cost
from vera_quant.models import Bar, Fill, Order, OrderStatus, OrderType, TransactionType
from vera_quant.money import round_to_tick

_STOP_TYPES = (OrderType.SL, OrderType.SL_M)
# Stops (adverse/protective) resolve before limits (favourable) when both
# trigger in the same bar for the same instrument.
_TYPE_PRIORITY = {OrderType.SL: 0, OrderType.SL_M: 0, OrderType.MARKET: 1, OrderType.LIMIT: 2}


@dataclass
class PaperBroker:
    slippage_ticks: Decimal
    cost_schedule: CostRateSchedule
    is_option: bool = False
    _pending: list[Order] = field(default_factory=list)

    def submit_order(self, order: Order) -> tuple[str | None, OrderStatus]:
        self._pending.append(order)
        return None, OrderStatus.OPEN

    def on_bar(self, bar: Bar) -> list[Fill]:
        relevant = [o for o in self._pending if o.instrument.symbol_token == bar.instrument_token]
        other = [o for o in self._pending if o.instrument.symbol_token != bar.instrument_token]
        relevant.sort(key=lambda o: _TYPE_PRIORITY[o.order_type])

        fills: list[Fill] = []
        still_pending: list[Order] = []
        for order in relevant:
            fill_price = self._resolve_fill_price(order, bar)
            if fill_price is None:
                still_pending.append(order)
                continue
            fills.append(self._make_fill(order, fill_price, bar))

        self._pending = other + still_pending
        return fills

    def _apply_slippage(self, price: Decimal, side: TransactionType, tick_size: Decimal) -> Decimal:
        adjustment = self.slippage_ticks * tick_size
        adjusted = price + adjustment if side == TransactionType.BUY else price - adjustment
        return round_to_tick(adjusted, tick_size)

    def _resolve_fill_price(self, order: Order, bar: Bar) -> Decimal | None:
        tick_size = order.instrument.tick_size

        if order.order_type == OrderType.MARKET:
            return self._apply_slippage(bar.open, order.transaction_type, tick_size)

        if order.order_type == OrderType.LIMIT:
            assert order.limit_price is not None
            limit = order.limit_price
            if order.transaction_type == TransactionType.BUY:
                if bar.open <= limit:
                    return bar.open
                if bar.low <= limit:
                    return limit
                return None
            else:
                if bar.open >= limit:
                    return bar.open
                if bar.high >= limit:
                    return limit
                return None

        if order.order_type in _STOP_TYPES:
            assert order.limit_price is not None  # the stop trigger level
            stop = order.limit_price
            if order.transaction_type == TransactionType.BUY:
                if bar.open >= stop:
                    return self._apply_slippage(bar.open, order.transaction_type, tick_size)
                if bar.high >= stop:
                    return self._apply_slippage(stop, order.transaction_type, tick_size)
                return None
            else:
                if bar.open <= stop:
                    return self._apply_slippage(bar.open, order.transaction_type, tick_size)
                if bar.low <= stop:
                    return self._apply_slippage(stop, order.transaction_type, tick_size)
                return None

        raise ValueError(f"unsupported order type {order.order_type}")

    def _make_fill(self, order: Order, fill_price: Decimal, bar: Bar) -> Fill:
        cost = compute_trade_cost(
            instrument=order.instrument,
            transaction_type=order.transaction_type,
            price=fill_price,
            quantity=order.quantity,
            is_option=self.is_option,
            schedule=self.cost_schedule,
        )
        return Fill(
            order_idempotency_key=order.idempotency_key,
            instrument=order.instrument,
            transaction_type=order.transaction_type,
            quantity=order.quantity,
            fill_price=fill_price,
            fees=cost.total,
            filled_at=bar.timestamp,
        )
