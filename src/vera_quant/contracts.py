"""Contract master loading, unit normalisation, expiry and rollover.

The raw Angel One scrip-master JSON encodes `tick_size` (and `strike`, for
options) as the real value times 100 — see the strike handling in
`reference/01-v6.py`. This module is the one place that divides those raw
fields back down; nothing else should touch them directly.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from vera_quant.models import Instrument

_EXPIRY_FORMAT = "%d%b%Y"  # e.g. "05DEC2025"

# P&L multiplier by underlying commodity name, for MCX contracts whose quoted
# unit differs from the traded lot unit (e.g. Gold: quoted per 10g, traded in
# 1kg lots -> ₹1 on the quote = ₹100 per lot). Verify against the exchange's
# current contract specification before trusting an entry for a commodity
# not yet listed here; it only applies on MCX — NSE F&O multiplier is 1.
MCX_QUOTATION_MULTIPLIER: dict[str, Decimal] = {
    "GOLD": Decimal(100),  # quoted per 10g, lot = 1kg
}


def _parse_raw_tick_size(raw: str) -> Decimal:
    """Angel One encodes tick_size as (actual tick size * 100)."""
    return Decimal(raw) / 100


def _parse_expiry(raw: str) -> date | None:
    if not raw:
        return None
    return datetime.strptime(raw, _EXPIRY_FORMAT).date()


def row_to_instrument(row: dict[str, str]) -> Instrument:
    """Convert one raw scrip-master row into a typed, unit-normalised Instrument."""
    exchange = row["exch_seg"]
    name = row.get("name", "")
    multiplier = MCX_QUOTATION_MULTIPLIER.get(name, Decimal(1)) if exchange == "MCX" else Decimal(1)
    return Instrument(
        exchange=exchange,
        trading_symbol=row["symbol"],
        symbol_token=row["token"],
        tick_size=_parse_raw_tick_size(row["tick_size"]),
        lot_size=int(row["lotsize"]),
        quotation_multiplier=multiplier,
        expiry=_parse_expiry(row.get("expiry", "")),
    )


class ScripMaster:
    """In-memory lookup over a loaded scrip-master snapshot.

    Built from a saved JSON snapshot in tests (and in backtest); in live
    mode the same snapshot is refreshed daily from Angel One's scrip-master
    URL and loaded the same way (see Appendix A / step 12).
    """

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._instruments = [row_to_instrument(r) for r in rows]
        self._by_token: dict[str, Instrument] = {i.symbol_token: i for i in self._instruments}
        self._by_symbol: dict[tuple[str, str], Instrument] = {
            (i.exchange, i.trading_symbol): i for i in self._instruments
        }

    @classmethod
    def from_file(cls, path: Path) -> ScripMaster:
        with open(path) as f:
            rows = json.load(f)
        return cls(rows)

    def get_by_token(self, token: str) -> Instrument | None:
        return self._by_token.get(token)

    def get_by_symbol(self, exchange: str, trading_symbol: str) -> Instrument | None:
        return self._by_symbol.get((exchange, trading_symbol))

    def futures_chain(self, exchange: str, name: str) -> list[Instrument]:
        """All futures instruments for one underlying, sorted by expiry ascending."""
        matches = [
            i
            for i in self._instruments
            if i.exchange == exchange and i.trading_symbol.startswith(name) and i.expiry is not None
        ]
        return sorted(matches, key=lambda i: i.expiry or date.max)


def days_to_expiry(expiry: date, as_of: date) -> int:
    return (expiry - as_of).days


def select_active_contract(
    chain: list[Instrument], as_of: date, roll_buffer_days: int
) -> Instrument:
    """Pick the contract that should be "front month" on `as_of`.

    Rolls out of a contract once fewer than `roll_buffer_days` remain to its
    expiry, moving to the next expiry in the chain — this is also how an MCX
    staggered-delivery window is modelled: pass a larger `roll_buffer_days`
    for physical-delivery commodities so the roll happens before delivery
    risk starts, not just before expiry. `chain` must be sorted by expiry
    ascending (as `ScripMaster.futures_chain` returns it).
    """
    if not chain:
        raise ValueError("empty contract chain")
    for instrument in chain:
        assert instrument.expiry is not None
        if days_to_expiry(instrument.expiry, as_of) >= roll_buffer_days:
            return instrument
    # Every contract is inside the roll window (or expired) — fall back to
    # the furthest-dated one rather than an already-expired contract.
    return chain[-1]
