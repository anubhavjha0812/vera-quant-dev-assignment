"""The one place money is rounded and converted.

Prices and P&L are always `Decimal` — never `float` (CLAUDE.md rule 1). This
module is the only place that quantizes a price to an instrument's tick size
or converts between rupees and integer paise for storage; nothing else
should round or quantize a money value directly.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PAISE_PER_RUPEE = Decimal(100)


def round_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round `price` to the nearest multiple of `tick_size` (half rounds up).

    This is the single rounding policy for every price in the system.
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")
    ticks = (price / tick_size).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return ticks * tick_size


def to_paise(amount: Decimal) -> int:
    """Convert a rupee amount to integer paise, for storage (e.g. the journal)."""
    return int((amount * PAISE_PER_RUPEE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_paise(paise: int) -> Decimal:
    """Convert integer paise back to a rupee `Decimal` amount."""
    return Decimal(paise) / PAISE_PER_RUPEE
