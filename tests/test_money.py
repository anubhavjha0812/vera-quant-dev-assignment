from decimal import Decimal

import pytest

from vera_quant.money import from_paise, round_to_tick, to_paise


def test_round_to_tick_rounds_down_when_below_half_tick() -> None:
    assert round_to_tick(Decimal("103.27"), Decimal("0.05")) == Decimal("103.25")


def test_round_to_tick_rounds_up_when_above_half_tick() -> None:
    assert round_to_tick(Decimal("103.28"), Decimal("0.05")) == Decimal("103.30")


def test_round_to_tick_exact_multiple_is_unchanged() -> None:
    assert round_to_tick(Decimal("103.25"), Decimal("0.05")) == Decimal("103.25")


def test_round_to_tick_half_tie_rounds_up() -> None:
    # 103.275 is exactly halfway between 103.25 and 103.30 at a 0.05 tick.
    assert round_to_tick(Decimal("103.275"), Decimal("0.05")) == Decimal("103.30")


def test_round_to_tick_rejects_non_positive_tick_size() -> None:
    with pytest.raises(ValueError):
        round_to_tick(Decimal("100"), Decimal("0"))


def test_round_to_tick_returns_decimal() -> None:
    result = round_to_tick(Decimal("100.03"), Decimal("0.05"))
    assert isinstance(result, Decimal)


def test_to_paise_and_from_paise_round_trip() -> None:
    amount = Decimal("1234.56")
    assert to_paise(amount) == 123456
    assert from_paise(123456) == amount


def test_to_paise_rounds_half_up() -> None:
    assert to_paise(Decimal("1.005")) == 101  # half-paisa rounds up
