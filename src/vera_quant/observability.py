"""Trade blotter export (desk spreadsheet layout) and a small helper that
attaches the order tag as a correlation ID to log records — both read
naturally alongside `backtest.TradeBlotterEntry` and
`order_management`'s idempotency key, which IS the order tag sent to
SmartAPI (steps 10/12): logging that same string consistently as
`order_tag` is what lets a trade be traced end-to-end through the logs.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from vera_quant.backtest import TradeBlotterEntry

_BLOTTER_COLUMNS = ["Date", "Time", "Symbol", "Side", "Quantity", "Price", "Fees", "Reason"]


def write_blotter_csv(blotter: list[TradeBlotterEntry], path: Path) -> None:
    """The desk's spreadsheet layout — one row per fill, opens directly in
    Excel/Sheets."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_BLOTTER_COLUMNS)
        for entry in blotter:
            writer.writerow(
                [
                    entry.timestamp.date().isoformat(),
                    entry.timestamp.time().isoformat(),
                    entry.trading_symbol,
                    entry.transaction_type,
                    entry.quantity,
                    str(entry.fill_price),
                    str(entry.fees),
                    entry.reason,
                ]
            )


def log_order_event(
    logger: logging.Logger,
    level: int,
    message: str,
    order_tag: str,
    **extra_fields: object,
) -> None:
    """Every order-related log call should go through this, so the order
    tag is always present under the same key (`order_tag`) and the JSON
    formatter (step 1) picks it up via `extra_fields`.
    """
    logger.log(level, message, extra={"extra_fields": {"order_tag": order_tag, **extra_fields}})
