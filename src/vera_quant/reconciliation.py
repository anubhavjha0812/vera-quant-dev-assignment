"""Reconciliation: backtest vs live/paper vs a hand-built desk
spreadsheet, to the paisa — the comparison itself uses exact Decimal
equality (CLAUDE.md rule 1's acceptance bar applied to the check, not just
the numbers being checked), never a "roughly matches" tolerance.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from vera_quant.models import Fill


@dataclass(frozen=True)
class ReferenceTrade:
    instrument_token: str
    side: str
    quantity: int
    price: Decimal


def load_reference_spreadsheet(path: Path) -> list[ReferenceTrade]:
    """A hand-built CSV the desk keeps — columns: instrument_token, side,
    quantity, price."""
    trades = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            trades.append(
                ReferenceTrade(
                    instrument_token=row["instrument_token"],
                    side=row["side"],
                    quantity=int(row["quantity"]),
                    price=Decimal(row["price"]),
                )
            )
    return trades


_Key = tuple[str, str, int, Decimal]


def _fill_key(instrument_token: str, side: str, quantity: int, price: Decimal) -> _Key:
    return (instrument_token, side, quantity, price)


@dataclass
class ReconciliationReport:
    matches: bool
    discrepancies: list[str] = field(default_factory=list)


def reconcile_trades(
    backtest_fills: list[Fill],
    live_fills: list[Fill],
    reference_trades: list[ReferenceTrade],
) -> ReconciliationReport:
    """Compares all three sources trade-by-trade, to the paisa. Order
    doesn't matter between sources (different pipelines may log in a
    different sequence) — counts and the exact (instrument, side,
    quantity, price) multiset must agree across all three.
    """
    backtest_keys = Counter(
        _fill_key(f.instrument.symbol_token, f.transaction_type.value, f.quantity, f.fill_price)
        for f in backtest_fills
    )
    live_keys = Counter(
        _fill_key(f.instrument.symbol_token, f.transaction_type.value, f.quantity, f.fill_price)
        for f in live_fills
    )
    reference_keys = Counter(
        _fill_key(t.instrument_token, t.side, t.quantity, t.price) for t in reference_trades
    )

    discrepancies: list[str] = []
    all_keys = set(backtest_keys) | set(live_keys) | set(reference_keys)
    for key in sorted(all_keys, key=str):
        b, live_count, r = backtest_keys[key], live_keys[key], reference_keys[key]
        if not (b == live_count == r):
            discrepancies.append(f"{key}: backtest={b}, live={live_count}, reference={r}")

    return ReconciliationReport(matches=not discrepancies, discrepancies=discrepancies)
