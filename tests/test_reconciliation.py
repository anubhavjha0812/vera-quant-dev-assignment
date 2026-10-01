from datetime import datetime
from decimal import Decimal
from pathlib import Path

from vera_quant.models import Instrument, TransactionType
from vera_quant.reconciliation import (
    ReferenceTrade,
    load_reference_spreadsheet,
    reconcile_trades,
)

FIXTURE = Path(__file__).parent / "fixtures" / "reference_trades.csv"


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


def _fill(side: TransactionType, qty: int, price: str):
    from vera_quant.models import Fill

    return Fill(
        order_idempotency_key="k",
        instrument=_instrument(),
        transaction_type=side,
        quantity=qty,
        fill_price=Decimal(price),
        fees=Decimal("0"),
        filled_at=datetime(2026, 1, 2, 9, 20),
    )


def test_load_reference_spreadsheet_parses_rows() -> None:
    trades = load_reference_spreadsheet(FIXTURE)
    assert len(trades) == 2
    assert trades[0].price == Decimal("100.50")


def test_reconcile_trades_shows_zero_difference_when_all_three_agree() -> None:
    fills = [
        _fill(TransactionType.BUY, 25, "100.50"),
        _fill(TransactionType.SELL, 10, "101.25"),
    ]
    reference = load_reference_spreadsheet(FIXTURE)

    report = reconcile_trades(backtest_fills=fills, live_fills=fills, reference_trades=reference)
    assert report.matches is True
    assert report.discrepancies == []


def _reference_100_50() -> list[ReferenceTrade]:
    return [
        ReferenceTrade(instrument_token="26000", side="BUY", quantity=25, price=Decimal("100.50"))
    ]


def test_reconcile_trades_flags_a_price_discrepancy_to_the_paisa() -> None:
    fills = [_fill(TransactionType.BUY, 25, "100.51")]  # one paisa off from reference
    reference = _reference_100_50()

    report = reconcile_trades(backtest_fills=fills, live_fills=fills, reference_trades=reference)
    assert report.matches is False
    # Exact ("to the paisa") matching reports both facts as separate line
    # items: the reference's 100.50 that nothing matched, and the fills'
    # 100.51 that the reference didn't have — not fuzzily merged into one.
    assert len(report.discrepancies) == 2


def test_reconcile_trades_flags_a_fill_missing_from_live() -> None:
    backtest_fills = [_fill(TransactionType.BUY, 25, "100.50")]
    reference = _reference_100_50()

    report = reconcile_trades(
        backtest_fills=backtest_fills, live_fills=[], reference_trades=reference
    )
    assert report.matches is False


def test_reconcile_trades_flags_a_quantity_discrepancy() -> None:
    backtest_fills = [_fill(TransactionType.BUY, 25, "100.50")]
    live_fills = [_fill(TransactionType.BUY, 20, "100.50")]
    reference = _reference_100_50()

    report = reconcile_trades(
        backtest_fills=backtest_fills, live_fills=live_fills, reference_trades=reference
    )
    assert report.matches is False
