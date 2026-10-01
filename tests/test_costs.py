"""Tests for the cost-model calculation structure, plus (at the bottom) a
check that `CostRateSchedule`'s real default rates compute exactly what
hand-calculation from the sourced figures says they should (see
`costs.py`'s module docstring for sourcing/date and the still-pending
Angel-One-calculator/contract-note verification).

SYNTHETIC_SCHEDULE below uses made-up round numbers — NOT real rates —
purely to exercise the formulas independent of what the real numbers
happen to be.
"""
from decimal import Decimal

import pytest

from vera_quant.costs import (
    CostRateSchedule,
    InstrumentSegment,
    compute_trade_cost,
    segment_for,
)
from vera_quant.models import Instrument, TransactionType

SYNTHETIC_SCHEDULE = CostRateSchedule(
    brokerage_flat_per_order=Decimal("20"),
    brokerage_pct_of_turnover=Decimal("0.0003"),
    stt_futures_sell_pct=Decimal("0.0002"),
    stt_options_sell_pct_of_premium=Decimal("0.001"),
    ctt_futures_sell_pct=Decimal("0.0001"),
    ctt_options_sell_pct_of_premium=Decimal("0.0005"),
    exchange_txn_pct_nse_futures=Decimal("0.000019"),
    exchange_txn_pct_nse_options_of_premium=Decimal("0.0005"),
    exchange_txn_pct_mcx_futures=Decimal("0.000019"),
    exchange_txn_pct_mcx_options_of_premium=Decimal("0.0005"),
    sebi_fee_pct=Decimal("0.000001"),
    stamp_duty_buy_pct_futures=Decimal("0.00002"),
    stamp_duty_buy_pct_options_of_premium=Decimal("0.00003"),
    gst_pct=Decimal("0.18"),
)


def _nfo_future() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


def _mcx_future() -> Instrument:
    return Instrument(
        exchange="MCX",
        trading_symbol="GOLD25DECFUT",
        symbol_token="441250",
        tick_size=Decimal("1"),
        lot_size=1,
        quotation_multiplier=Decimal(100),
    )


def test_segment_for_routes_nse_and_mcx_correctly() -> None:
    assert segment_for(_nfo_future(), is_option=False) == InstrumentSegment.NSE_FUTURES
    assert segment_for(_nfo_future(), is_option=True) == InstrumentSegment.NSE_OPTIONS
    assert segment_for(_mcx_future(), is_option=False) == InstrumentSegment.MCX_FUTURES
    assert segment_for(_mcx_future(), is_option=True) == InstrumentSegment.MCX_OPTIONS


def test_segment_for_rejects_unknown_exchange() -> None:
    bad = Instrument(
        exchange="BSE", trading_symbol="X", symbol_token="1", tick_size=Decimal("1"), lot_size=1
    )
    with pytest.raises(ValueError):
        segment_for(bad, is_option=False)


def test_brokerage_charges_whichever_is_lower() -> None:
    # Small turnover: flat (20) < pct-based -> pct-based should win (be charged).
    small = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.BUY,
        price=Decimal("10"),
        quantity=1,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    # 10 * 0.0003 = 0.003 -> rounds to 0.00, well below the flat fee.
    assert small.brokerage == Decimal("0.00")

    # Large turnover: pct-based (large) > flat (20) -> flat should win.
    large = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.BUY,
        price=Decimal("1000000"),
        quantity=1,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    assert large.brokerage == Decimal("20.00")


def test_stt_only_charged_on_sell_side_for_nse_futures() -> None:
    sell = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("100"),
        quantity=25,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    buy = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.BUY,
        price=Decimal("100"),
        quantity=25,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    assert sell.stt > Decimal("0")
    assert buy.stt == Decimal("0")
    assert sell.ctt == Decimal("0")  # NSE instrument never carries CTT


def test_ctt_only_charged_on_sell_side_for_mcx_futures() -> None:
    sell = compute_trade_cost(
        instrument=_mcx_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("50000"),
        quantity=1,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    buy = compute_trade_cost(
        instrument=_mcx_future(),
        transaction_type=TransactionType.BUY,
        price=Decimal("50000"),
        quantity=1,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    assert sell.ctt > Decimal("0")
    assert buy.ctt == Decimal("0")
    assert sell.stt == Decimal("0")  # MCX instrument never carries STT


def test_exchange_transaction_charge_applies_on_both_sides() -> None:
    for txn_type in (TransactionType.BUY, TransactionType.SELL):
        leg = compute_trade_cost(
            instrument=_nfo_future(),
            transaction_type=txn_type,
            price=Decimal("100"),
            quantity=25,
            is_option=False,
            schedule=SYNTHETIC_SCHEDULE,
        )
        assert leg.exchange_txn_charge > Decimal("0")


def test_stamp_duty_only_on_buy_side() -> None:
    buy = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.BUY,
        price=Decimal("100"),
        quantity=25,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    sell = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("100"),
        quantity=25,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    assert buy.stamp_duty > Decimal("0")
    assert sell.stamp_duty == Decimal("0")


def test_gst_is_charged_only_on_brokerage_exchange_txn_and_sebi_fee() -> None:
    leg = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("1000000"),
        quantity=1,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    expected_gst_base = leg.brokerage + leg.exchange_txn_charge + leg.sebi_fee
    expected_gst = (expected_gst_base * SYNTHETIC_SCHEDULE.gst_pct).quantize(Decimal("0.01"))
    assert leg.gst == expected_gst
    # GST must NOT be charged on STT/CTT/stamp duty.
    wrong_base = expected_gst_base + leg.stt + leg.stamp_duty
    wrong_gst = (wrong_base * SYNTHETIC_SCHEDULE.gst_pct).quantize(Decimal("0.01"))
    assert leg.gst != wrong_gst


def test_total_sums_every_component() -> None:
    leg = compute_trade_cost(
        instrument=_mcx_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("50000"),
        quantity=1,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    expected = (
        leg.brokerage
        + leg.stt
        + leg.ctt
        + leg.exchange_txn_charge
        + leg.sebi_fee
        + leg.stamp_duty
        + leg.gst
    )
    assert leg.total == expected


def test_every_component_is_rounded_to_the_paisa() -> None:
    leg = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("333.33"),
        quantity=25,
        is_option=False,
        schedule=SYNTHETIC_SCHEDULE,
    )
    for component in (
        leg.brokerage,
        leg.stt,
        leg.ctt,
        leg.exchange_txn_charge,
        leg.sebi_fee,
        leg.stamp_duty,
        leg.gst,
    ):
        assert component == component.quantize(Decimal("0.01"))


# ----------------------------------------------------- real default rates
#
# CostRateSchedule() with no overrides = the sourced rates in costs.py
# (web-searched 2026-10-01, dated and cited there). These tests hand-
# calculate the expected cost from those documented percentages and
# confirm the engine matches exactly — i.e. the defaults really do encode
# what the module docstring claims. This is NOT the same as verification
# against a real Angel One contract note (still pending from the user).


def test_real_schedule_nse_future_sell_matches_hand_calculation() -> None:
    # SELL 25 qty @ 20,000 -> turnover = 5,00,000.
    leg = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("20000"),
        quantity=25,
        is_option=False,
        schedule=CostRateSchedule(),
    )
    turnover = Decimal("500000")
    assert leg.brokerage == Decimal("20.00")  # flat, F&O
    assert leg.stt == (turnover * Decimal("0.0005")).quantize(Decimal("0.01"))  # 250.00
    assert leg.ctt == Decimal("0.00")
    assert leg.exchange_txn_charge == (turnover * Decimal("0.000019")).quantize(Decimal("0.01"))
    assert leg.sebi_fee == (turnover * Decimal("0.0000001")).quantize(Decimal("0.01"))
    assert leg.stamp_duty == Decimal("0.00")  # sell side, never charged
    gst_base = leg.brokerage + leg.exchange_txn_charge + leg.sebi_fee
    assert leg.gst == (gst_base * Decimal("0.18")).quantize(Decimal("0.01"))


def test_real_schedule_nse_future_buy_has_stamp_duty_not_stt() -> None:
    leg = compute_trade_cost(
        instrument=_nfo_future(),
        transaction_type=TransactionType.BUY,
        price=Decimal("20000"),
        quantity=25,
        is_option=False,
        schedule=CostRateSchedule(),
    )
    turnover = Decimal("500000")
    assert leg.stt == Decimal("0.00")  # STT is sell-side only
    assert leg.stamp_duty == (turnover * Decimal("0.00002")).quantize(Decimal("0.01"))  # 10.00


def test_real_schedule_mcx_future_sell_charges_ctt_not_stt() -> None:
    # SELL 1 lot @ 70,000 -> turnover = 70,000.
    leg = compute_trade_cost(
        instrument=_mcx_future(),
        transaction_type=TransactionType.SELL,
        price=Decimal("70000"),
        quantity=1,
        is_option=False,
        schedule=CostRateSchedule(),
    )
    turnover = Decimal("70000")
    assert leg.stt == Decimal("0.00")
    assert leg.ctt == (turnover * Decimal("0.0001")).quantize(Decimal("0.01"))  # 7.00
    assert leg.exchange_txn_charge == (turnover * Decimal("0.000021")).quantize(Decimal("0.01"))


def test_real_schedule_gst_rate_is_eighteen_percent() -> None:
    assert CostRateSchedule().gst_pct == Decimal("0.18")
