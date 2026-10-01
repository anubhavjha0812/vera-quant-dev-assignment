from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from vera_quant.contracts import (
    ScripMaster,
    days_to_expiry,
    row_to_instrument,
    select_active_contract,
)

FIXTURE = Path(__file__).parent / "fixtures" / "scrip_master_sample.json"


def _gold_row(tick_size: str = "100.000000") -> dict:
    return {
        "token": "441250",
        "symbol": "GOLD25DECFUT",
        "name": "GOLD",
        "expiry": "05DEC2025",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "FUTCOM",
        "exch_seg": "MCX",
        "tick_size": tick_size,
    }


def test_tick_size_is_divided_by_100() -> None:
    # Angel One encodes tick_size as (actual tick size * 100).
    instrument = row_to_instrument(_gold_row(tick_size="100.000000"))
    assert instrument.tick_size == Decimal("1.00")

    nifty_row = {
        "token": "26000",
        "symbol": "NIFTY25DECFUT",
        "name": "NIFTY",
        "expiry": "30DEC2025",
        "strike": "-1.000000",
        "lotsize": "25",
        "instrumenttype": "FUTIDX",
        "exch_seg": "NFO",
        "tick_size": "5.000000",
    }
    assert row_to_instrument(nifty_row).tick_size == Decimal("0.05")


def test_known_mcx_commodity_gets_its_multiplier() -> None:
    instrument = row_to_instrument(_gold_row())
    assert instrument.quotation_multiplier == Decimal(100)


def test_unlisted_mcx_commodity_defaults_multiplier_to_one() -> None:
    row = _gold_row()
    row["name"] = "SOMENEWCOMMODITY"
    row["symbol"] = "SOMENEWCOMMODITY26FEBFUT"
    instrument = row_to_instrument(row)
    assert instrument.quotation_multiplier == Decimal(1)


def test_nse_instrument_multiplier_is_always_one_even_if_name_collides() -> None:
    row = {
        "token": "99999",
        "symbol": "GOLD25DECFUT",  # same symbol text, but on NFO not MCX
        "name": "GOLD",
        "expiry": "30DEC2025",
        "strike": "-1.000000",
        "lotsize": "25",
        "instrumenttype": "FUTSTK",
        "exch_seg": "NFO",
        "tick_size": "5.000000",
    }
    assert row_to_instrument(row).quotation_multiplier == Decimal(1)


def test_expiry_is_parsed_to_a_date() -> None:
    instrument = row_to_instrument(_gold_row())
    assert instrument.expiry == date(2025, 12, 5)


def test_scrip_master_lookups_by_token_and_symbol() -> None:
    master = ScripMaster.from_file(FIXTURE)
    by_token = master.get_by_token("441250")
    assert by_token is not None
    assert by_token.trading_symbol == "GOLD25DECFUT"

    by_symbol = master.get_by_symbol("MCX", "GOLD25DECFUT")
    assert by_symbol is not None
    assert by_symbol.symbol_token == "441250"

    assert master.get_by_token("does-not-exist") is None


def test_futures_chain_is_sorted_by_expiry() -> None:
    master = ScripMaster.from_file(FIXTURE)
    chain = master.futures_chain("MCX", "GOLD")
    assert [i.trading_symbol for i in chain] == ["GOLD25DECFUT", "GOLD26FEBFUT"]


def test_days_to_expiry() -> None:
    assert days_to_expiry(date(2025, 12, 10), date(2025, 12, 1)) == 9
    assert days_to_expiry(date(2025, 12, 1), date(2025, 12, 1)) == 0


def test_select_active_contract_picks_front_month_outside_roll_window() -> None:
    master = ScripMaster.from_file(FIXTURE)
    chain = master.futures_chain("MCX", "GOLD")
    active = select_active_contract(chain, as_of=date(2025, 11, 1), roll_buffer_days=5)
    assert active.trading_symbol == "GOLD25DECFUT"


def test_select_active_contract_rolls_inside_roll_window() -> None:
    master = ScripMaster.from_file(FIXTURE)
    chain = master.futures_chain("MCX", "GOLD")
    # 3 days to expiry of the front contract, with a 5-day roll buffer.
    active = select_active_contract(chain, as_of=date(2025, 12, 2), roll_buffer_days=5)
    assert active.trading_symbol == "GOLD26FEBFUT"


def test_select_active_contract_falls_back_to_last_when_all_inside_window() -> None:
    master = ScripMaster.from_file(FIXTURE)
    chain = master.futures_chain("MCX", "GOLD")
    # Both contracts expired/near-expired relative to this date.
    active = select_active_contract(chain, as_of=date(2026, 2, 4), roll_buffer_days=5)
    assert active.trading_symbol == "GOLD26FEBFUT"


def test_select_active_contract_rejects_empty_chain() -> None:
    with pytest.raises(ValueError):
        select_active_contract([], as_of=date(2025, 1, 1), roll_buffer_days=5)
