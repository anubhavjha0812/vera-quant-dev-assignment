import csv
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from vera_quant.backtest import TradeBlotterEntry
from vera_quant.observability import log_order_event, write_blotter_csv


def test_write_blotter_csv_desk_layout(tmp_path: Path) -> None:
    blotter = [
        TradeBlotterEntry(
            timestamp=datetime(2026, 1, 2, 9, 20, 0),
            instrument_token="26000",
            trading_symbol="NIFTY25DECFUT",
            transaction_type="BUY",
            quantity=25,
            fill_price=Decimal("100.50"),
            fees=Decimal("12.34"),
            reason="grid_entry_level_1",
        )
    ]
    path = tmp_path / "blotter.csv"
    write_blotter_csv(blotter, path)

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]
    assert row["Symbol"] == "NIFTY25DECFUT"
    assert row["Side"] == "BUY"
    assert row["Quantity"] == "25"
    assert row["Price"] == "100.50"
    assert row["Fees"] == "12.34"
    assert row["Reason"] == "grid_entry_level_1"
    assert row["Date"] == "2026-01-02"


def test_write_blotter_csv_empty_blotter_still_writes_header(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    write_blotter_csv([], path)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == []


def test_log_order_event_carries_order_tag_as_correlation_id(caplog) -> None:  # type: ignore[no-untyped-def]
    logger = logging.getLogger("test.observability")
    with caplog.at_level(logging.INFO, logger="test.observability"):
        log_order_event(logger, logging.INFO, "order placed", order_tag="abc123", symbol="NIFTY")
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.extra_fields["order_tag"] == "abc123"  # type: ignore[attr-defined]
    assert record.extra_fields["symbol"] == "NIFTY"  # type: ignore[attr-defined]
