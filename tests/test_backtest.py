"""End-to-end backtest harness tests. Step 11's Done-when: a full backtest
runs end to end and the no-lookahead + golden-file tests pass.
"""
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from vera_quant.backtest import (
    BacktestResult,
    make_walk_forward_windows,
    run_backtest,
    run_walk_forward,
)
from vera_quant.brokers.paper import PaperBroker
from vera_quant.costs import CostRateSchedule
from vera_quant.indicators import Atr
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Bar, Instrument, OrderIntent, Position
from vera_quant.risk import RiskParams, RiskState, risk_gate
from vera_quant.strategies import SarParams, SarState, sar_step

GOLDEN_FILE = Path(__file__).parent / "fixtures" / "golden_backtest_result.json"


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


def _risk_params() -> RiskParams:
    return RiskParams(
        max_position_per_instrument=1000,
        max_total_position=1000,
        max_order_size=1000,
        max_daily_loss=Decimal("-1000000"),
        max_drawdown_pct=Decimal("0.99"),
        reject_storm_max_rejects=999,
        reject_storm_window_seconds=60,
        stale_data_max_seconds=999999,
    )


def _make_sar_decide() -> tuple:
    """Fresh ATR + SAR state per call, so each backtest run is independent."""
    atr = Atr(period=5)
    state = SarState()
    params = SarParams(k_multiplier=Decimal("2.0"), quantity=1)

    def decide(bar: Bar, position: Position) -> list[OrderIntent]:
        atr_value = atr.update(bar)
        return sar_step(bar, atr_value or Decimal(0), position, state, params)

    return decide


def _make_risk_check():
    risk_state = RiskState(last_data_timestamp=None)
    params = _risk_params()

    def risk_check(intent: OrderIntent, position: Position, now: datetime) -> OrderIntent | None:
        risk_state.last_data_timestamp = now
        total = abs(position.net_quantity)
        return risk_gate(intent, position, total, risk_state, params, now=now)

    return risk_check


def _make_broker() -> PaperBroker:
    return PaperBroker(slippage_ticks=Decimal(0), cost_schedule=CostRateSchedule(), is_option=False)


# ------------------------------------------------------------------ basics


def test_run_backtest_produces_fills_and_a_final_position() -> None:
    bars = generate_synthetic_bars(
        instrument_token="26000",
        start=datetime(2026, 1, 2, 9, 15),
        periods=40,
        pattern="trend_up",
        seed=30,
    )
    result = run_backtest(
        bars, _instrument(), _make_sar_decide(), _make_risk_check(), _make_broker()
    )
    assert len(result.fills) >= 1  # the initial SAR entry, at minimum
    assert result.final_position is not None
    assert len(result.equity_curve) == len(bars)
    assert len(result.blotter) == len(result.fills)


# ------------------------------------------------------------- no lookahead


def test_no_lookahead_truncated_run_matches_full_run_up_to_the_cutoff() -> None:
    bars = generate_synthetic_bars(
        instrument_token="26000",
        start=datetime(2026, 1, 2, 9, 15),
        periods=60,
        pattern="chop",
        seed=31,
    )
    cutoff = 40

    full = run_backtest(bars, _instrument(), _make_sar_decide(), _make_risk_check(), _make_broker())
    truncated = run_backtest(
        bars[:cutoff], _instrument(), _make_sar_decide(), _make_risk_check(), _make_broker()
    )

    assert len(truncated.fills) <= len(full.fills)
    assert truncated.fills == full.fills[: len(truncated.fills)]
    assert truncated.equity_curve == full.equity_curve[: len(truncated.equity_curve)]


# --------------------------------------------------------------- walk-forward


def test_walk_forward_windows_are_rolling_and_cover_every_bar_out_of_sample_once() -> None:
    bars = generate_synthetic_bars(
        instrument_token="26000", start=datetime(2026, 1, 2, 9, 15), periods=100, seed=32
    )
    windows = make_walk_forward_windows(bars, in_sample_size=20, out_of_sample_size=10)
    assert len(windows) > 0
    for window in windows:
        assert len(window.in_sample) == 20
        assert len(window.out_of_sample) == 10

    # Out-of-sample segments tile the series with no gaps or overlaps.
    all_oos_timestamps = [b.timestamp for w in windows for b in w.out_of_sample]
    assert all_oos_timestamps == sorted(set(all_oos_timestamps))


def test_walk_forward_rejects_non_positive_window_sizes() -> None:
    bars = generate_synthetic_bars(
        instrument_token="26000", start=datetime(2026, 1, 2, 9, 15), periods=10, seed=33
    )
    with pytest.raises(ValueError):
        make_walk_forward_windows(bars, in_sample_size=0, out_of_sample_size=5)


def test_run_walk_forward_produces_one_result_per_window() -> None:
    bars = generate_synthetic_bars(
        instrument_token="26000", start=datetime(2026, 1, 2, 9, 15), periods=80, seed=34
    )
    results = run_walk_forward(
        bars,
        _instrument(),
        in_sample_size=20,
        out_of_sample_size=10,
        decide_factory=_make_sar_decide,
        risk_check=_make_risk_check(),
        broker_factory=_make_broker,
    )
    expected_windows = make_walk_forward_windows(bars, 20, 10)
    assert len(results) == len(expected_windows)
    assert all(isinstance(r, BacktestResult) for r in results)


# ----------------------------------------------------------------- golden file


def _run_golden_backtest() -> BacktestResult:
    bars = generate_synthetic_bars(
        instrument_token="26000",
        start=datetime(2026, 1, 2, 9, 15),
        periods=50,
        pattern="chop",
        seed=99,
    )
    return run_backtest(bars, _instrument(), _make_sar_decide(), _make_risk_check(), _make_broker())


def _serialize(result: BacktestResult) -> dict:
    assert result.final_position is not None
    return {
        "fill_count": len(result.fills),
        "fills": [
            {
                "side": f.transaction_type.value,
                "quantity": f.quantity,
                "price": str(f.fill_price),
                "fees": str(f.fees),
            }
            for f in result.fills
        ],
        "final_net_quantity": result.final_position.net_quantity,
        "final_realized_pnl": str(result.final_position.realized_pnl),
    }


def test_golden_file_regression() -> None:
    result = _run_golden_backtest()
    actual = _serialize(result)
    with open(GOLDEN_FILE) as f:
        expected = json.load(f)
    assert actual == expected
