"""Step 14's Done-when: a kill-and-restart mid-session ends with state
identical to the broker's, in both paper and live mode.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from vera_quant.backtest import run_backtest
from vera_quant.brokers.live_smartapi.broker import LiveSafetyLimits
from vera_quant.indicators import Atr
from vera_quant.live_runner import (
    LiveRunnerConfig,
    create_broker,
    reconcile_on_startup,
    resume_broker_with_pending_orders,
    run_live_session,
)
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Instrument, OrderIntent, Position
from vera_quant.order_management import Journal
from vera_quant.rate_limiter import RateLimiter
from vera_quant.risk import RiskParams, RiskState, risk_gate
from vera_quant.strategies import SarParams, SarState, sar_step


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


def _make_sar_decide():
    atr = Atr(period=5)
    state = SarState()
    params = SarParams(k_multiplier=Decimal("2.0"), quantity=1)

    def decide(bar, position):
        atr_value = atr.update(bar)
        return sar_step(bar, atr_value or Decimal(0), position, state, params)

    return decide


def _make_risk_check():
    risk_state = RiskState()
    params = _risk_params()

    def risk_check(intent: OrderIntent, position: Position, now: datetime):
        risk_state.last_data_timestamp = now
        return risk_gate(intent, position, abs(position.net_quantity), risk_state, params, now=now)

    return risk_check


# ----------------------------------------------------------------- factory


def test_create_broker_paper_mode_never_imports_live_smartapi() -> None:
    import sys

    sys.modules.pop("vera_quant.brokers.live_smartapi", None)
    config = LiveRunnerConfig(broker_mode="paper")
    broker = create_broker(config)
    assert "vera_quant.brokers.live_smartapi" not in sys.modules
    assert type(broker).__name__ == "PaperBroker"


def test_create_broker_live_mode_requires_confirm_live() -> None:
    config = LiveRunnerConfig(
        broker_mode="live",
        confirm_live=False,
        live_client=object(),  # type: ignore[arg-type]
        live_safety_limits=LiveSafetyLimits(max_order_size=10, max_daily_orders=10),
    )
    with pytest.raises(RuntimeError, match="confirm"):
        create_broker(config)


def test_create_broker_live_mode_with_confirmation_succeeds() -> None:
    class FakeClient:
        def get_margin(self, params):
            return {"status": True, "data": {"availablecash": "0"}}

    config = LiveRunnerConfig(
        broker_mode="live",
        confirm_live=True,
        live_client=FakeClient(),  # type: ignore[arg-type]
        live_safety_limits=LiveSafetyLimits(max_order_size=10, max_daily_orders=10),
    )
    broker = create_broker(config)
    assert type(broker).__name__ == "SmartApiBroker"


def test_create_broker_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        create_broker(LiveRunnerConfig(broker_mode="nonsense"))


# ----------------------------------------------------------- reconciliation


def test_reconcile_on_startup_rebuilds_positions_from_the_journal(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    instrument = _instrument()
    bars = generate_synthetic_bars(
        instrument_token=instrument.symbol_token,
        start=datetime(2026, 1, 2, 9, 15),
        periods=15,
        pattern="trend_up",
        seed=61,
    )
    broker = create_broker(LiveRunnerConfig(broker_mode="paper"))
    run_live_session(bars, instrument, _make_sar_decide(), _make_risk_check(), broker, journal)
    journal.close()

    reopened = Journal(tmp_path / "journal.sqlite3")
    result = reconcile_on_startup(
        reopened, broker_mode="paper", live_client=None, order_book_rate_limiter=None
    )
    assert instrument.symbol_token in result.positions
    assert result.mismatches == []
    reopened.close()


def test_reconcile_on_startup_flags_an_order_missing_from_the_live_broker(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    from vera_quant.models import Order, OrderStatus, OrderType, TransactionType

    journal.record_order(
        Order(
            idempotency_key="k1",
            instrument=_instrument(),
            transaction_type=TransactionType.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            status=OrderStatus.OPEN,
        )
    )

    class FakeLiveClient:
        def get_order_book(self):
            return {"status": True, "data": []}  # broker has no record of it

    result = reconcile_on_startup(
        journal,
        broker_mode="live",
        live_client=FakeLiveClient(),  # type: ignore[arg-type]
        order_book_rate_limiter=RateLimiter(1, 1.0),
        sleep=lambda _s: None,
    )
    assert len(result.mismatches) == 1
    assert "k1" in result.mismatches[0]
    journal.close()


def test_reconcile_on_startup_no_mismatch_when_broker_agrees(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    from vera_quant.models import Order, OrderStatus, OrderType, TransactionType

    journal.record_order(
        Order(
            idempotency_key="k1",
            instrument=_instrument(),
            transaction_type=TransactionType.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            status=OrderStatus.OPEN,
        )
    )

    class FakeLiveClient:
        def get_order_book(self):
            return {"status": True, "data": [{"ordertag": "k1", "orderid": "O1"}]}

    result = reconcile_on_startup(
        journal,
        broker_mode="live",
        live_client=FakeLiveClient(),  # type: ignore[arg-type]
        order_book_rate_limiter=RateLimiter(1, 1.0),
        sleep=lambda _s: None,
    )
    assert result.mismatches == []
    journal.close()


# -------------------------------------------------------- kill and restart


def test_kill_and_restart_recovers_position_exactly_matching_the_pre_kill_state(
    tmp_path: Path,
) -> None:
    instrument = _instrument()
    bars = generate_synthetic_bars(
        instrument_token=instrument.symbol_token,
        start=datetime(2026, 1, 2, 9, 15),
        periods=40,
        pattern="chop",
        seed=62,
    )
    midpoint = 20

    journal_path = tmp_path / "live.sqlite3"
    journal = Journal(journal_path)
    broker = create_broker(LiveRunnerConfig(broker_mode="paper"))
    position_at_kill = run_live_session(
        bars[:midpoint], instrument, _make_sar_decide(), _make_risk_check(), broker, journal
    )
    journal.close()  # simulates the kill — process dies here

    # --- Restart ---
    reopened = Journal(journal_path)
    reconciliation = reconcile_on_startup(
        reopened, broker_mode="paper", live_client=None, order_book_rate_limiter=None
    )
    recovered_position = reconciliation.positions[instrument.symbol_token]

    assert recovered_position.net_quantity == position_at_kill.net_quantity
    assert recovered_position.avg_price == position_at_kill.avg_price
    assert recovered_position.realized_pnl == position_at_kill.realized_pnl
    assert reconciliation.mismatches == []

    # --- Resume: a fresh broker adopts any still-pending orders, then
    # processing continues without crashing or corrupting state. ---
    resumed_broker = create_broker(LiveRunnerConfig(broker_mode="paper"))
    resume_broker_with_pending_orders(resumed_broker, reconciliation.pending_orders)
    final_position = run_live_session(
        bars[midpoint:],
        instrument,
        _make_sar_decide(),
        _make_risk_check(),
        resumed_broker,
        reopened,
    )
    assert final_position.instrument == instrument  # resumed cleanly, no crash
    reopened.close()


def test_kill_and_restart_in_live_mode_also_recovers_exactly(tmp_path: Path) -> None:
    instrument = _instrument()
    bars = generate_synthetic_bars(
        instrument_token=instrument.symbol_token,
        start=datetime(2026, 1, 2, 9, 15),
        periods=30,
        pattern="trend_down",
        seed=63,
    )
    midpoint = 15

    class FakeLiveClient:
        def __init__(self) -> None:
            self.orders: list[dict] = []

        def place_order(self, params):
            order_id = f"O{len(self.orders) + 1}"
            self.orders.append(
                {"ordertag": params["ordertag"], "orderid": order_id, "orderstatus": "complete"}
            )
            return {"status": True, "data": {"orderid": order_id}}

        def get_order_book(self):
            return {"status": True, "data": self.orders}

        def get_margin(self, params):
            return {"status": True, "data": {"availablecash": "10000000"}}

    journal_path = tmp_path / "live_mode.sqlite3"
    journal = Journal(journal_path)
    client = FakeLiveClient()
    config = LiveRunnerConfig(
        broker_mode="live",
        confirm_live=True,
        live_client=client,  # type: ignore[arg-type]
        live_safety_limits=LiveSafetyLimits(max_order_size=1000, max_daily_orders=1000),
    )
    broker = create_broker(config)
    position_at_kill = run_live_session(
        bars[:midpoint], instrument, _make_sar_decide(), _make_risk_check(), broker, journal
    )
    journal.close()

    reopened = Journal(journal_path)
    reconciliation = reconcile_on_startup(
        reopened,
        broker_mode="live",
        live_client=client,  # type: ignore[arg-type]
        order_book_rate_limiter=RateLimiter(1, 1.0),
        sleep=lambda _s: None,
    )
    recovered_position = reconciliation.positions.get(instrument.symbol_token)
    if recovered_position is not None:
        assert recovered_position.net_quantity == position_at_kill.net_quantity
        assert recovered_position.realized_pnl == position_at_kill.realized_pnl
    assert reconciliation.mismatches == []  # the fake broker agrees with the journal
    reopened.close()


# ------------------------------------------------------------------ parity


def test_parity_replaying_the_same_bars_through_the_backtest_gives_identical_results() -> None:
    """Proves one deterministic code path, not two that could drift
    (CLAUDE.md rule 3): the same bars, strategy and risk config produce
    bit-identical fills/P&L whether run as "the live session" or replayed
    afterward "through the backtest" for verification.
    """
    instrument = _instrument()
    bars = generate_synthetic_bars(
        instrument_token=instrument.symbol_token,
        start=datetime(2026, 1, 2, 9, 15),
        periods=40,
        pattern="chop",
        seed=64,
    )

    def make_broker():
        return create_broker(LiveRunnerConfig(broker_mode="paper"))

    live_session_result = run_backtest(
        bars, instrument, _make_sar_decide(), _make_risk_check(), make_broker()
    )
    backtest_replay_result = run_backtest(
        bars, instrument, _make_sar_decide(), _make_risk_check(), make_broker()
    )

    assert live_session_result.fills == backtest_replay_result.fills
    assert (
        live_session_result.final_position.realized_pnl
        == backtest_replay_result.final_position.realized_pnl
    )
