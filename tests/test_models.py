from dataclasses import FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal

import pytest

from vera_quant.models import (
    Bar,
    Fill,
    Instrument,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    Tick,
    TransactionType,
)


def _instrument() -> Instrument:
    return Instrument(
        exchange="MCX",
        trading_symbol="GOLD25DECFUT",
        symbol_token="12345",
        tick_size=Decimal("1"),
        lot_size=1,
        quotation_multiplier=Decimal("100"),
        expiry=date(2025, 12, 31),
    )


def test_instrument_fields_are_decimal_and_typed() -> None:
    inst = _instrument()
    assert isinstance(inst.tick_size, Decimal)
    assert isinstance(inst.quotation_multiplier, Decimal)
    assert inst.lot_size == 1


def test_bar_money_fields_are_decimal() -> None:
    bar = Bar(
        instrument_token="12345",
        timestamp=datetime(2026, 1, 2, 9, 15),
        open=Decimal("100.5"),
        high=Decimal("101.0"),
        low=Decimal("99.5"),
        close=Decimal("100.75"),
        volume=1000,
    )
    for value in (bar.open, bar.high, bar.low, bar.close):
        assert isinstance(value, Decimal)


def test_tick_defaults_last_quantity_to_zero() -> None:
    tick = Tick(instrument_token="12345", timestamp=datetime.now(), last_price=Decimal("100"))
    assert tick.last_quantity == 0


def test_order_intent_is_immutable() -> None:
    intent = OrderIntent(
        instrument=_instrument(),
        transaction_type=TransactionType.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100.5"),
    )
    with pytest.raises(FrozenInstanceError):
        intent.quantity = 2  # type: ignore


def test_order_defaults_to_pending_status() -> None:
    order = Order(
        idempotency_key="abc123",
        instrument=_instrument(),
        transaction_type=TransactionType.SELL,
        quantity=2,
        order_type=OrderType.MARKET,
    )
    assert order.status == OrderStatus.PENDING


def test_fill_and_position_fields_are_decimal() -> None:
    fill = Fill(
        order_idempotency_key="abc123",
        instrument=_instrument(),
        transaction_type=TransactionType.BUY,
        quantity=1,
        fill_price=Decimal("100.5"),
        fees=Decimal("12.34"),
        filled_at=datetime.now(),
    )
    assert isinstance(fill.fill_price, Decimal)
    assert isinstance(fill.fees, Decimal)

    position = Position(instrument=_instrument())
    assert position.net_quantity == 0
    assert isinstance(position.avg_price, Decimal)
    assert isinstance(position.realized_pnl, Decimal)
