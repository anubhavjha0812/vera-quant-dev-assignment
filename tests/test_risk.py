"""Step 9's Done-when: tests show caps clipping orders and each kill switch
halting trading.

Flatten behavior is a parked decision (DECISIONS.md, "Still pending") —
`flatten_intents()` exists and is tested on its own, but no kill switch
wires it in automatically yet; every switch here only blocks new orders.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from vera_quant.models import Instrument, OrderIntent, OrderType, Position, TransactionType
from vera_quant.risk import RiskParams, RiskState, check_kill_switches, flatten_intents, risk_gate

NOW = datetime(2026, 1, 2, 10, 0, 0)


def _instrument(token: str = "1") -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol=f"SYM{token}",
        symbol_token=token,
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


def _intent(
    instrument: Instrument, qty: int, side: TransactionType = TransactionType.BUY
) -> OrderIntent:
    return OrderIntent(
        instrument=instrument,
        transaction_type=side,
        quantity=qty,
        order_type=OrderType.MARKET,
    )


def _params(**overrides: object) -> RiskParams:
    defaults: dict[str, object] = dict(
        max_position_per_instrument=10,
        max_total_position=20,
        max_order_size=5,
        max_daily_loss=Decimal("-10000"),
        max_drawdown_pct=Decimal("0.20"),
        reject_storm_max_rejects=3,
        reject_storm_window_seconds=60,
        stale_data_max_seconds=30,
    )
    defaults.update(overrides)
    return RiskParams(**defaults)  # type: ignore[arg-type]


def _gate(
    intent: OrderIntent,
    position: Position,
    total_position: int,
    state: RiskState,
    params: RiskParams,
) -> OrderIntent | None:
    return risk_gate(intent, position, total_position, state, params, now=NOW)


# ----------------------------------------------------------------- clipping


def test_max_order_size_clips_an_oversized_order() -> None:
    inst = _instrument()
    state = RiskState()
    params = _params(max_order_size=5)
    intent = _intent(inst, qty=20)
    result = _gate(intent, Position(instrument=inst), 0, state, params)
    assert result is not None
    assert result.quantity == 5


def test_per_instrument_cap_clips_to_remaining_room() -> None:
    inst = _instrument()
    state = RiskState()
    params = _params(max_position_per_instrument=10, max_order_size=100)
    position = Position(instrument=inst, net_quantity=8)
    intent = _intent(inst, qty=7)  # would take position to 15, cap is 10
    result = _gate(intent, position, 8, state, params)
    assert result is not None
    assert result.quantity == 2  # only 2 more fit under the cap


def test_per_instrument_cap_blocks_entirely_when_already_at_cap() -> None:
    inst = _instrument()
    state = RiskState()
    params = _params(max_position_per_instrument=10, max_order_size=100)
    position = Position(instrument=inst, net_quantity=10)
    intent = _intent(inst, qty=5)
    result = _gate(intent, position, 10, state, params)
    assert result is None


def test_total_position_cap_clips_across_instruments() -> None:
    inst = _instrument("2")
    state = RiskState()
    params = _params(max_total_position=20, max_position_per_instrument=100, max_order_size=100)
    position = Position(instrument=inst, net_quantity=0)
    intent = _intent(inst, qty=10)
    # total_position already at 18 from other instruments -> only 2 more room
    result = _gate(intent, position, 18, state, params)
    assert result is not None
    assert result.quantity == 2


def test_sell_intents_reduce_exposure_and_are_never_clipped_by_caps() -> None:
    inst = _instrument()
    state = RiskState()
    params = _params(max_position_per_instrument=10, max_order_size=100)
    position = Position(instrument=inst, net_quantity=10)  # already at cap, long
    intent = _intent(inst, qty=8, side=TransactionType.SELL)  # reduces exposure
    result = _gate(intent, position, 10, state, params)
    assert result is not None
    assert result.quantity == 8


# ------------------------------------------------------------- kill switches


def test_manual_kill_switch_blocks_new_orders() -> None:
    inst = _instrument()
    state = RiskState(manual_kill=True)
    params = _params()
    result = _gate(_intent(inst, qty=1), Position(instrument=inst), 0, state, params)
    assert result is None
    assert "manual" in check_kill_switches(state, params, now=NOW)


def test_max_daily_loss_switch_trips_and_blocks() -> None:
    inst = _instrument()
    state = RiskState(daily_pnl=Decimal("-15000"))
    params = _params(max_daily_loss=Decimal("-10000"))
    tripped = check_kill_switches(state, params, now=NOW)
    assert "max_daily_loss" in tripped
    result = _gate(_intent(inst, qty=1), Position(instrument=inst), 0, state, params)
    assert result is None


def test_max_drawdown_switch_trips_and_blocks() -> None:
    inst = _instrument()
    state = RiskState(peak_equity=Decimal("100000"), current_equity=Decimal("75000"))
    params = _params(max_drawdown_pct=Decimal("0.20"))  # 25% drawdown > 20% limit
    tripped = check_kill_switches(state, params, now=NOW)
    assert "max_drawdown" in tripped
    result = _gate(_intent(inst, qty=1), Position(instrument=inst), 0, state, params)
    assert result is None


def test_reject_storm_switch_trips_after_threshold_rejects_in_window() -> None:
    inst = _instrument()
    state = RiskState(
        recent_rejects=[NOW - timedelta(seconds=10), NOW - timedelta(seconds=5), NOW]
    )
    params = _params(reject_storm_max_rejects=3, reject_storm_window_seconds=60)
    tripped = check_kill_switches(state, params, now=NOW)
    assert "reject_storm" in tripped
    result = _gate(_intent(inst, qty=1), Position(instrument=inst), 0, state, params)
    assert result is None


def test_reject_storm_switch_ignores_rejects_outside_the_window() -> None:
    state = RiskState(recent_rejects=[NOW - timedelta(seconds=120), NOW - timedelta(seconds=90)])
    params = _params(reject_storm_max_rejects=2, reject_storm_window_seconds=60)
    assert "reject_storm" not in check_kill_switches(state, params, now=NOW)


def test_stale_data_switch_trips_when_feed_is_old() -> None:
    inst = _instrument()
    state = RiskState(last_data_timestamp=NOW - timedelta(seconds=60))
    params = _params(stale_data_max_seconds=30)
    tripped = check_kill_switches(state, params, now=NOW)
    assert "stale_data" in tripped
    result = _gate(_intent(inst, qty=1), Position(instrument=inst), 0, state, params)
    assert result is None


def test_stale_data_switch_does_not_trip_with_a_fresh_feed() -> None:
    state = RiskState(last_data_timestamp=NOW - timedelta(seconds=5))
    params = _params(stale_data_max_seconds=30)
    assert "stale_data" not in check_kill_switches(state, params, now=NOW)


def test_no_switches_tripped_allows_normal_flow_with_caps_still_applied() -> None:
    inst = _instrument()
    state = RiskState(
        daily_pnl=Decimal("500"),
        peak_equity=Decimal("100000"),
        current_equity=Decimal("100500"),
        last_data_timestamp=NOW,
    )
    params = _params()
    assert check_kill_switches(state, params, now=NOW) == set()
    result = _gate(_intent(inst, qty=3), Position(instrument=inst), 0, state, params)
    assert result is not None
    assert result.quantity == 3


# -------------------------------------------------------------------- flatten


def test_flatten_intents_closes_every_nonzero_position() -> None:
    long_inst = _instrument("long")
    short_inst = _instrument("short")
    flat_inst = _instrument("flat")
    positions = [
        Position(instrument=long_inst, net_quantity=10),
        Position(instrument=short_inst, net_quantity=-4),
        Position(instrument=flat_inst, net_quantity=0),
    ]
    intents = flatten_intents(positions, reason="manual_kill")

    assert len(intents) == 2
    by_symbol = {i.instrument.trading_symbol: i for i in intents}
    assert by_symbol[long_inst.trading_symbol].transaction_type == TransactionType.SELL
    assert by_symbol[long_inst.trading_symbol].quantity == 10
    assert by_symbol[short_inst.trading_symbol].transaction_type == TransactionType.BUY
    assert by_symbol[short_inst.trading_symbol].quantity == 4
    assert all(i.reason == "manual_kill" for i in intents)
