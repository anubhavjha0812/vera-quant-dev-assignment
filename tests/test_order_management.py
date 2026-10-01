"""Step 10's Done-when: duplicate submissions are no-ops; a crash mid-order
recovers cleanly; P&L from fills matches expected.
"""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

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
from vera_quant.order_management import (
    InvalidTransitionError,
    Journal,
    apply_fill,
    make_idempotency_key,
    place_idempotent,
    rebuild_positions_from_fills,
    transition,
)

ORDERTAG_MAX_LENGTH = 15  # SmartAPI forum-reported limit; see dev plan Appendix A


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
        expiry=date(2025, 12, 30),
    )


def _intent(qty: int = 25, side: TransactionType = TransactionType.BUY) -> OrderIntent:
    return OrderIntent(
        instrument=_instrument(), transaction_type=side, quantity=qty, order_type=OrderType.MARKET
    )


def _fill(
    qty: int, price: str, side: TransactionType = TransactionType.BUY, fees: str = "0"
) -> Fill:
    return Fill(
        order_idempotency_key="k",
        instrument=_instrument(),
        transaction_type=side,
        quantity=qty,
        fill_price=Decimal(price),
        fees=Decimal(fees),
        filled_at=datetime(2026, 1, 2, 10, 0),
    )


# ------------------------------------------------------------- idempotency


def test_idempotency_key_fits_smartapi_ordertag_length() -> None:
    key = make_idempotency_key("intent-1", _intent())
    assert len(key) <= ORDERTAG_MAX_LENGTH


def test_idempotency_key_is_deterministic() -> None:
    key1 = make_idempotency_key("intent-1", _intent())
    key2 = make_idempotency_key("intent-1", _intent())
    assert key1 == key2


def test_idempotency_key_differs_for_different_intent_ids() -> None:
    key1 = make_idempotency_key("intent-1", _intent())
    key2 = make_idempotency_key("intent-2", _intent())
    assert key1 != key2


def test_idempotency_key_differs_for_different_intent_contents() -> None:
    a = make_idempotency_key("intent-1", _intent(qty=25))
    b = make_idempotency_key("intent-1", _intent(qty=50))
    assert a != b


# ---------------------------------------------------------- state machine


def test_valid_transition_succeeds() -> None:
    order = Order(
        idempotency_key="k",
        instrument=_instrument(),
        transaction_type=TransactionType.BUY,
        quantity=25,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
    )
    transition(order, OrderStatus.OPEN)
    assert order.status == OrderStatus.OPEN


def test_invalid_transition_raises() -> None:
    order = Order(
        idempotency_key="k",
        instrument=_instrument(),
        transaction_type=TransactionType.BUY,
        quantity=25,
        order_type=OrderType.MARKET,
        status=OrderStatus.COMPLETE,
    )
    with pytest.raises(InvalidTransitionError):
        transition(order, OrderStatus.OPEN)


def test_terminal_states_accept_no_further_transitions() -> None:
    for terminal in (OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED):
        order = Order(
            idempotency_key="k",
            instrument=_instrument(),
            transaction_type=TransactionType.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            status=terminal,
        )
        with pytest.raises(InvalidTransitionError):
            transition(order, OrderStatus.PENDING)


# ---------------------------------------------------- journal + idempotent placement


def test_duplicate_submission_with_same_intent_id_is_a_no_op(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    send_calls = []

    def fake_send(order: Order) -> tuple[str | None, OrderStatus]:
        send_calls.append(order)
        return "BROKER123", OrderStatus.COMPLETE

    order1 = place_idempotent("intent-1", _intent(), journal, send=fake_send)
    order2 = place_idempotent("intent-1", _intent(), journal, send=fake_send)

    assert len(send_calls) == 1
    assert order1.idempotency_key == order2.idempotency_key
    assert order2.status == OrderStatus.COMPLETE
    assert order2.broker_order_id == "BROKER123"
    journal.close()


def test_different_intent_ids_both_send() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        journal = Journal(Path(tmp) / "journal.sqlite3")
        send_calls = []

        def fake_send(order: Order) -> tuple[str | None, OrderStatus]:
            send_calls.append(order)
            return f"BROKER{len(send_calls)}", OrderStatus.COMPLETE

        place_idempotent("intent-1", _intent(), journal, send=fake_send)
        place_idempotent("intent-2", _intent(), journal, send=fake_send)

        assert len(send_calls) == 2
        journal.close()


def test_crash_mid_order_leaves_a_recoverable_pending_record(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.sqlite3"
    journal = Journal(db_path)

    def crashing_send(order: Order) -> tuple[str | None, OrderStatus]:
        raise RuntimeError("simulated crash: broker never responded")

    with pytest.raises(RuntimeError):
        place_idempotent("intent-1", _intent(), journal, send=crashing_send)
    journal.close()  # simulates process death

    # "Restart": a fresh Journal instance opened on the same file.
    recovered = Journal(db_path)
    orders = recovered.all_orders()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.PENDING  # write-ahead record survived
    assert orders[0].instrument.symbol_token == _intent().instrument.symbol_token
    recovered.close()


def test_resubmitting_after_a_simulated_crash_does_not_resend(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.sqlite3"
    journal = Journal(db_path)

    def crashing_send(order: Order) -> tuple[str | None, OrderStatus]:
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        place_idempotent("intent-1", _intent(), journal, send=crashing_send)
    journal.close()

    # Process restarts, reopens the journal, and retries the SAME decision.
    recovered = Journal(db_path)
    send_calls = []

    def fake_send(order: Order) -> tuple[str | None, OrderStatus]:
        send_calls.append(order)
        return "BROKER1", OrderStatus.COMPLETE

    result = place_idempotent("intent-1", _intent(), recovered, send=fake_send)
    assert len(send_calls) == 0  # already journaled -> never resent
    assert result.status == OrderStatus.PENDING  # still whatever it was left at
    recovered.close()


def test_journal_order_round_trip_preserves_decimal_and_optional_fields(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    order = Order(
        idempotency_key="abc123",
        instrument=_instrument(),
        transaction_type=TransactionType.SELL,
        quantity=25,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("123.45"),
        status=OrderStatus.OPEN,
        broker_order_id="XYZ",
        created_at=datetime(2026, 1, 2, 9, 15),
    )
    journal.record_order(order)
    fetched = journal.get_order("abc123")
    assert fetched is not None
    assert fetched.limit_price == Decimal("123.45")
    assert isinstance(fetched.limit_price, Decimal)
    assert fetched.instrument.tick_size == Decimal("0.05")
    assert fetched.instrument.expiry == date(2025, 12, 30)
    assert fetched.status == OrderStatus.OPEN
    journal.close()


def test_journal_returns_none_for_unknown_key(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    assert journal.get_order("does-not-exist") is None
    journal.close()


def test_journal_fill_round_trip(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    fill = _fill(qty=10, price="100.25", fees="12.50")
    journal.record_fill(fill)
    fills = journal.all_fills()
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("100.25")
    assert fills[0].fees == Decimal("12.50")
    assert isinstance(fills[0].fill_price, Decimal)
    journal.close()


# --------------------------------------------------------- position / P&L


def test_apply_fill_opens_a_long_position() -> None:
    position = Position(instrument=_instrument())
    apply_fill(position, _fill(qty=10, price="100", side=TransactionType.BUY))
    assert position.net_quantity == 10
    assert position.avg_price == Decimal("100")
    assert position.realized_pnl == Decimal("0")


def test_apply_fill_adds_to_a_long_position_blends_avg_price() -> None:
    position = Position(instrument=_instrument(), net_quantity=10, avg_price=Decimal("100"))
    apply_fill(position, _fill(qty=10, price="110", side=TransactionType.BUY))
    assert position.net_quantity == 20
    assert position.avg_price == Decimal("105")  # (100*10 + 110*10) / 20


def test_apply_fill_partially_reduces_long_position_realizes_pnl() -> None:
    position = Position(instrument=_instrument(), net_quantity=10, avg_price=Decimal("100"))
    apply_fill(position, _fill(qty=4, price="110", side=TransactionType.SELL))
    assert position.net_quantity == 6
    assert position.avg_price == Decimal("100")  # unchanged for the remainder
    assert position.realized_pnl == Decimal("40")  # (110-100)*4


def test_apply_fill_closes_a_long_position_exactly_to_flat() -> None:
    position = Position(instrument=_instrument(), net_quantity=10, avg_price=Decimal("100"))
    apply_fill(position, _fill(qty=10, price="95", side=TransactionType.SELL))
    assert position.net_quantity == 0
    assert position.realized_pnl == Decimal("-50")  # (95-100)*10


def test_apply_fill_flips_a_long_position_through_flat_to_short() -> None:
    position = Position(instrument=_instrument(), net_quantity=10, avg_price=Decimal("100"))
    apply_fill(position, _fill(qty=15, price="105", side=TransactionType.SELL))
    assert position.net_quantity == -5
    assert position.realized_pnl == Decimal("50")  # (105-100)*10 on the closed part
    assert position.avg_price == Decimal("105")  # the new short leg opened at the fill price


def test_rebuild_positions_from_fills_matches_manual_calculation() -> None:
    inst = _instrument()
    fills = [
        Fill(
            order_idempotency_key="a",
            instrument=inst,
            transaction_type=TransactionType.BUY,
            quantity=10,
            fill_price=Decimal("100"),
            fees=Decimal("5"),
            filled_at=datetime(2026, 1, 2, 9, 15),
        ),
        Fill(
            order_idempotency_key="b",
            instrument=inst,
            transaction_type=TransactionType.BUY,
            quantity=10,
            fill_price=Decimal("110"),
            fees=Decimal("5"),
            filled_at=datetime(2026, 1, 2, 9, 20),
        ),
        Fill(
            order_idempotency_key="c",
            instrument=inst,
            transaction_type=TransactionType.SELL,
            quantity=15,
            fill_price=Decimal("115"),
            fees=Decimal("5"),
            filled_at=datetime(2026, 1, 2, 9, 25),
        ),
    ]
    positions = rebuild_positions_from_fills(fills)
    position = positions[inst.symbol_token]
    assert position.net_quantity == 5
    assert position.avg_price == Decimal("105")  # blended from the two buys
    # (115-105)*15 price P&L on the sell, minus 5+5+5 fees across all 3 fills
    assert position.realized_pnl == Decimal("135")
