"""Step 15's Done-when: tests cover state changes, overrides reaching the
strategy, and a breaker tripping; the backtest runs with the regime on
and off.
"""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from vera_quant.backtest import run_backtest
from vera_quant.brokers.paper import PaperBroker
from vera_quant.costs import CostRateSchedule
from vera_quant.indicators import Atr
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Bar, Instrument, OrderIntent, Position
from vera_quant.regime import (
    DEFAULT_REGIME_OVERRIDES,
    MacroObservation,
    MacroScorer,
    RegimeEngine,
    RegimeState,
    RegimeThresholds,
    apply_overrides_to_grid_params,
    apply_overrides_to_risk_params,
    latest_known_observation,
    load_macro_csv,
    next_regime_state,
)
from vera_quant.risk import RiskParams, RiskState, risk_gate
from vera_quant.strategies import GridParams

FIXTURE = Path(__file__).parent / "fixtures" / "macro_sample.csv"
PROXIES = ["india_vix", "usdinr"]


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


# -------------------------------------------------------------------- CSV


def test_load_macro_csv_parses_rows_sorted_by_known_at() -> None:
    observations = load_macro_csv(FIXTURE, PROXIES)
    assert len(observations) > 0
    assert observations == sorted(observations, key=lambda o: o.known_at)
    assert observations[0].values["india_vix"] > 0


def test_latest_known_observation_never_returns_future_data() -> None:
    observations = load_macro_csv(FIXTURE, PROXIES)
    cutoff = observations[2].known_at
    result = latest_known_observation(observations, as_of=cutoff)
    assert result is not None
    assert result.known_at <= cutoff
    # Nothing later than the cutoff could have been picked.
    assert result.known_at == cutoff or result.known_at < cutoff


def test_latest_known_observation_returns_none_before_any_data_exists() -> None:
    observations = load_macro_csv(FIXTURE, PROXIES)
    before_everything = observations[0].known_at.replace(year=2000)
    assert latest_known_observation(observations, as_of=before_everything) is None


# ----------------------------------------------------------------- scoring


def test_macro_scorer_returns_none_until_window_is_warm() -> None:
    scorer = MacroScorer(proxy_names=PROXIES, lookback=5)
    obs = MacroObservation(
        as_of_date=date(2026, 1, 2),
        known_at=datetime(2026, 1, 2, 18, 0),
        values={"india_vix": Decimal("14"), "usdinr": Decimal("83")},
    )
    for _ in range(4):
        assert scorer.update(obs) is None
    assert scorer.update(obs) is not None  # 5th observation warms it up


def test_macro_scorer_z_score_matches_manual_calculation() -> None:
    scorer = MacroScorer(proxy_names=["india_vix"], lookback=3)
    values = [Decimal("10"), Decimal("20"), Decimal("30")]
    result = None
    for v in values:
        obs = MacroObservation(
            as_of_date=date(2026, 1, 2), known_at=datetime(2026, 1, 2), values={"india_vix": v}
        )
        result = scorer.update(obs)
    assert result is not None
    # mean=20, population variance = ((10)^2+(0)+(10)^2)/3 = 66.67, std ~8.165
    # z-score of the last value (30): (30-20)/8.165 ~= 1.2247
    assert abs(result["india_vix"] - Decimal("1.2247")) < Decimal("0.001")


# -------------------------------------------------------------- hysteresis


def test_next_regime_state_enters_stressed_at_the_enter_threshold() -> None:
    thresholds = RegimeThresholds()
    assert (
        next_regime_state(RegimeState.NORMAL, Decimal("1.6"), thresholds) == RegimeState.STRESSED
    )
    assert next_regime_state(RegimeState.NORMAL, Decimal("1.4"), thresholds) == RegimeState.NORMAL


def test_next_regime_state_does_not_flap_inside_the_hysteresis_band() -> None:
    thresholds = RegimeThresholds()
    # Already STRESSED; a small dip that doesn't cross the (lower) exit
    # threshold must NOT bounce back to NORMAL.
    assert (
        next_regime_state(RegimeState.STRESSED, Decimal("1.2"), thresholds)
        == RegimeState.STRESSED
    )
    assert (
        next_regime_state(RegimeState.STRESSED, Decimal("0.9"), thresholds) == RegimeState.NORMAL
    )


def test_next_regime_state_escalates_to_crisis_and_back() -> None:
    thresholds = RegimeThresholds()
    assert (
        next_regime_state(RegimeState.STRESSED, Decimal("2.6"), thresholds) == RegimeState.CRISIS
    )
    # Dipping just under crisis_enter but still above crisis_exit stays CRISIS.
    assert next_regime_state(RegimeState.CRISIS, Decimal("2.3"), thresholds) == RegimeState.CRISIS
    assert next_regime_state(RegimeState.CRISIS, Decimal("1.9"), thresholds) == RegimeState.STRESSED


# -------------------------------------------------------------- the engine


def test_regime_engine_transitions_through_a_spike_and_back() -> None:
    # With a lookback of N and only the newest observation as an outlier
    # among N-1 identical calm values, the outlier's z-score approaches
    # (but never exceeds) sqrt(N-1) as the spike grows — for lookback=5
    # that ceiling is 2.0, comfortably above stressed_enter (1.5) but
    # below crisis_enter (2.5), so this test targets STRESSED specifically.
    engine = RegimeEngine(
        thresholds=RegimeThresholds(),
        scorer=MacroScorer(proxy_names=["india_vix"], lookback=5),
        weights={"india_vix": Decimal(1)},
    )
    calm_values = [Decimal("14")] * 5
    state = RegimeState.NORMAL
    for v in calm_values:
        state = engine.update(
            MacroObservation(
                as_of_date=date(2026, 1, 2), known_at=datetime(2026, 1, 2), values={"india_vix": v}
            )
        )
    assert state == RegimeState.NORMAL

    spike = MacroObservation(
        as_of_date=date(2026, 1, 3),
        known_at=datetime(2026, 1, 3),
        values={"india_vix": Decimal("1000")},
    )
    state = engine.update(spike)
    assert state == RegimeState.STRESSED


# -------------------------------------------------------------- overrides


def test_grid_overrides_widen_spacing_and_shrink_pyramiding_in_stressed() -> None:
    base = GridParams(k_spacing=Decimal("1.0"), max_units=4, quantity_per_unit=1)
    stressed = DEFAULT_REGIME_OVERRIDES[RegimeState.STRESSED]
    overridden = apply_overrides_to_grid_params(base, stressed)
    assert overridden.k_spacing > base.k_spacing
    assert overridden.max_units < base.max_units
    assert overridden.quantity_per_unit == base.quantity_per_unit  # untouched field passes through


def test_normal_regime_overrides_are_a_no_op() -> None:
    base = GridParams(k_spacing=Decimal("1.0"), max_units=4, quantity_per_unit=1)
    overridden = apply_overrides_to_grid_params(base, DEFAULT_REGIME_OVERRIDES[RegimeState.NORMAL])
    assert overridden == base


def test_risk_overrides_tighten_position_cap_in_crisis() -> None:
    base = RiskParams(
        max_position_per_instrument=20,
        max_total_position=50,
        max_order_size=10,
        max_daily_loss=Decimal("-10000"),
        max_drawdown_pct=Decimal("0.2"),
        reject_storm_max_rejects=3,
        reject_storm_window_seconds=60,
        stale_data_max_seconds=30,
    )
    overridden = apply_overrides_to_risk_params(base, DEFAULT_REGIME_OVERRIDES[RegimeState.CRISIS])
    assert overridden.max_position_per_instrument < base.max_position_per_instrument
    assert overridden.max_total_position == base.max_total_position  # unrelated field untouched


# ---------------------------------------------------------- circuit breaker


def test_regime_crisis_trips_the_circuit_breaker_and_blocks_new_orders() -> None:
    inst = _instrument()
    params = RiskParams(
        max_position_per_instrument=100,
        max_total_position=100,
        max_order_size=100,
        max_daily_loss=Decimal("-1000000"),
        max_drawdown_pct=Decimal("0.99"),
        reject_storm_max_rejects=999,
        reject_storm_window_seconds=60,
        stale_data_max_seconds=999999,
    )
    from vera_quant.models import TransactionType

    state = RiskState(regime_circuit_breaker=True)
    intent = OrderIntent(instrument=inst, transaction_type=TransactionType.BUY, quantity=1)
    now = datetime(2026, 1, 2, 10, 0)
    result = risk_gate(intent, Position(instrument=inst), 0, state, params, now=now)
    assert result is None


def test_regime_normal_does_not_trip_the_circuit_breaker() -> None:
    inst = _instrument()
    params = RiskParams(
        max_position_per_instrument=100,
        max_total_position=100,
        max_order_size=100,
        max_daily_loss=Decimal("-1000000"),
        max_drawdown_pct=Decimal("0.99"),
        reject_storm_max_rejects=999,
        reject_storm_window_seconds=60,
        stale_data_max_seconds=999999,
    )
    state = RiskState(regime_circuit_breaker=False)
    from vera_quant.models import TransactionType

    intent = OrderIntent(instrument=inst, transaction_type=TransactionType.BUY, quantity=1)
    now = datetime(2026, 1, 2, 10, 0)
    result = risk_gate(intent, Position(instrument=inst), 0, state, params, now=now)
    assert result is not None


# ------------------------------------------------------------ backtest on/off


def test_backtest_runs_to_completion_with_regime_on_and_off() -> None:
    instrument = _instrument()
    bars = generate_synthetic_bars(
        instrument_token=instrument.symbol_token,
        start=datetime(2026, 1, 2, 9, 15),
        periods=30,
        pattern="chop",
        seed=70,
    )

    from vera_quant.strategies import GridState, grid_step

    def make_decide(regime_on: bool):
        atr = Atr(period=5)
        grid_state = GridState()
        engine = RegimeEngine(
            thresholds=RegimeThresholds(),
            scorer=MacroScorer(proxy_names=["india_vix"], lookback=3),
            weights={"india_vix": Decimal(1)},
        )
        base_params = GridParams(k_spacing=Decimal("0.5"), max_units=5, quantity_per_unit=1)

        def decide(bar: Bar, position: Position) -> list[OrderIntent]:
            atr_value = atr.update(bar)
            params = base_params
            if regime_on:
                regime_state = engine.update(
                    MacroObservation(
                        as_of_date=bar.timestamp.date(),
                        known_at=bar.timestamp,
                        values={"india_vix": Decimal("14") + (bar.close - bar.open)},
                    )
                )
                params = apply_overrides_to_grid_params(
                    base_params, DEFAULT_REGIME_OVERRIDES[regime_state]
                )
            return grid_step(bar, atr_value or Decimal(0), position, grid_state, params)

        return decide

    def risk_check(intent: OrderIntent, position: Position, now: datetime):
        risk_params = RiskParams(
            max_position_per_instrument=100,
            max_total_position=100,
            max_order_size=100,
            max_daily_loss=Decimal("-1000000"),
            max_drawdown_pct=Decimal("0.99"),
            reject_storm_max_rejects=999,
            reject_storm_window_seconds=60,
            stale_data_max_seconds=999999,
        )
        total = abs(position.net_quantity)
        return risk_gate(intent, position, total, RiskState(), risk_params, now=now)

    for regime_on in (True, False):
        broker = PaperBroker(
            slippage_ticks=Decimal(0), cost_schedule=CostRateSchedule(), is_option=False
        )
        result = run_backtest(bars, instrument, make_decide(regime_on), risk_check, broker)
        assert result.final_position is not None  # ran to completion either way
