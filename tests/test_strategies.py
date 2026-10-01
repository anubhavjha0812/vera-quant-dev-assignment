"""Scenario tests for the grid and stop-and-reverse engines, per step 8's
Done-when: a trend, a choppy market, and a gap through several grid levels.
Synthetic bars come straight from step 6's generator.
"""
from datetime import datetime
from decimal import Decimal

from vera_quant.indicators import Atr
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Bar, Instrument, OrderIntent, Position, TransactionType
from vera_quant.strategies import GridParams, GridState, SarParams, SarState, grid_step, sar_step


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


def _position() -> Position:
    return Position(instrument=_instrument())


def _run_grid(bars: list[Bar], params: GridParams) -> tuple[list[list[OrderIntent]], GridState]:
    atr = Atr(period=5)
    state = GridState()
    position = _position()
    all_intents = []
    for bar in bars:
        atr_value = atr.update(bar)
        intents = grid_step(bar, atr_value or Decimal(0), position, state, params)
        all_intents.append(intents)
    return all_intents, state


def _run_sar(bars, params: SarParams):
    atr = Atr(period=5)
    state = SarState()
    position = _position()
    all_intents = []
    for bar in bars:
        atr_value = atr.update(bar)
        intents = sar_step(bar, atr_value or Decimal(0), position, state, params)
        all_intents.append(intents)
    return all_intents, state


# -------------------------------------------------------------------- grid --


def test_grid_pyramids_through_multiple_levels_in_a_pure_trend() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=60,
        pattern="trend_up",
        seed=1,
    )
    params = GridParams(k_spacing=Decimal("1.0"), max_units=4, quantity_per_unit=1)
    all_intents, state = _run_grid(bars, params)

    flat = [i for bar_intents in all_intents for i in bar_intents]
    buys = [i for i in flat if i.transaction_type == TransactionType.BUY]
    sells = [i for i in flat if i.transaction_type == TransactionType.SELL]

    # Strictly increasing close -> offset_levels only grows -> no retraces.
    assert len(sells) == 0
    assert state.filled_levels == params.max_units
    assert len(buys) == params.max_units


def test_grid_respects_max_units_cap_even_in_a_long_trend() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=200,
        pattern="trend_up",
        seed=2,
    )
    params = GridParams(k_spacing=Decimal("0.5"), max_units=3, quantity_per_unit=1)
    _, state = _run_grid(bars, params)
    assert state.filled_levels == params.max_units


def test_grid_gap_fills_several_levels_in_one_bar() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=30, pattern="gap", seed=3
    )
    params = GridParams(k_spacing=Decimal("0.3"), max_units=10, quantity_per_unit=1)
    all_intents, _ = _run_grid(bars, params)
    max_in_one_bar = max((len(intents) for intents in all_intents), default=0)
    assert max_in_one_bar > 1, "expected the gap bar to cross more than one grid level at once"


def test_grid_chop_produces_entries_and_exits_and_stays_within_cap() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=100, pattern="chop", seed=4
    )
    params = GridParams(k_spacing=Decimal("0.5"), max_units=5, quantity_per_unit=1)
    all_intents, state = _run_grid(bars, params)

    flat = [i for bar_intents in all_intents for i in bar_intents]
    entries = [i for i in flat if "entry" in i.reason]
    exits = [i for i in flat if "exit" in i.reason]

    assert len(entries) > 0
    assert len(exits) > 0  # chop must retrace at least once
    assert 0 <= state.filled_levels <= params.max_units


def test_grid_does_nothing_while_atr_is_not_warmed_up() -> None:
    bar = generate_synthetic_bars(
        instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=1, seed=5
    )[0]
    params = GridParams()
    state = GridState()
    intents = grid_step(bar, Decimal(0), _position(), state, params)
    assert intents == []
    assert state.anchor is None


# --------------------------------------------------------------------- SAR --


def test_sar_enters_long_on_the_first_real_bar() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=10,
        pattern="trend_up",
        seed=6,
    )
    params = SarParams(k_multiplier=Decimal("2.0"), quantity=1)
    all_intents, state = _run_sar(bars, params)
    assert all_intents[0][0].transaction_type == TransactionType.BUY
    assert all_intents[0][0].quantity == 1
    assert state.direction == 1


def test_sar_stays_long_through_a_pure_uptrend_no_flip() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=50,
        pattern="trend_up",
        seed=7,
    )
    params = SarParams(k_multiplier=Decimal("2.0"), quantity=1)
    all_intents, state = _run_sar(bars, params)
    flat = [i for bar_intents in all_intents for i in bar_intents]
    assert len(flat) == 1  # only the initial entry, never stopped out
    assert state.direction == 1


def test_sar_flips_to_short_on_a_sharp_reversal() -> None:
    up = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=20,
        pattern="trend_up",
        seed=8,
    )
    down = generate_synthetic_bars(
        instrument_token="1",
        start=up[-1].timestamp,
        periods=20,
        pattern="trend_down",
        seed=8,
        base_price=up[-1].close,
    )
    bars = up + down[1:]
    params = SarParams(k_multiplier=Decimal("1.0"), quantity=1)
    all_intents, state = _run_sar(bars, params)
    flat = [i for bar_intents in all_intents for i in bar_intents]

    flips = [i for i in flat if i.reason == "sar_flip_to_short"]
    assert len(flips) >= 1
    assert flips[0].quantity == params.quantity * 2
    assert state.direction == -1


def test_sar_does_nothing_while_atr_is_not_warmed_up() -> None:
    bar = generate_synthetic_bars(
        instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=1, seed=9
    )[0]
    state = SarState()
    intents = sar_step(bar, Decimal(0), _position(), state, SarParams())
    assert intents == []
    assert state.direction == 0
