"""CLI entry points. `poetry run demo-backtest` (see pyproject.toml's
[tool.poetry.scripts]) or `python scripts/demo_backtest.py` both land here.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from vera_quant.backtest import run_backtest
from vera_quant.brokers.paper import PaperBroker
from vera_quant.costs import CostRateSchedule
from vera_quant.indicators import Atr
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Bar, Instrument, OrderIntent, Position
from vera_quant.risk import RiskParams, RiskState, risk_gate
from vera_quant.strategies import SarParams, SarState, sar_step


def demo_backtest() -> None:
    """One command, no network, no credentials: synthetic data -> ATR+SAR
    -> risk layer -> PaperBroker -> a full backtest. Step 17's Done-when,
    the backtest half: "a fresh clone runs ... a demo backtest with one
    command."
    """
    instrument = Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )
    bars = generate_synthetic_bars(
        instrument_token="26000",
        start=datetime(2026, 1, 2, 9, 15),
        periods=200,
        pattern="chop",
        seed=1,
    )

    atr = Atr(period=14)
    sar_state = SarState()
    sar_params = SarParams(k_multiplier=Decimal("2.0"), quantity=1)

    def decide(bar: Bar, position: Position) -> list[OrderIntent]:
        atr_value = atr.update(bar)
        return sar_step(bar, atr_value or Decimal(0), position, sar_state, sar_params)

    risk_state = RiskState()
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

    def risk_check(
        intent: OrderIntent, position: Position, now: datetime
    ) -> OrderIntent | None:
        risk_state.last_data_timestamp = now
        total = abs(position.net_quantity)
        return risk_gate(intent, position, total, risk_state, risk_params, now=now)

    broker = PaperBroker(
        slippage_ticks=Decimal(1), cost_schedule=CostRateSchedule(), is_option=False
    )
    result = run_backtest(bars, instrument, decide, risk_check, broker)

    print(f"Bars processed:   {len(bars)}")
    print(f"Fills:            {len(result.fills)}")
    assert result.final_position is not None
    print(
        f"Final position:   {result.final_position.net_quantity} @ "
        f"{result.final_position.avg_price}"
    )
    print(f"Realized P&L:     {result.final_position.realized_pnl}")
    print()
    print("Last 5 blotter entries:")
    for entry in result.blotter[-5:]:
        print(
            f"  {entry.timestamp}  {entry.transaction_type:5s} "
            f"{entry.quantity:>4d} @ {entry.fill_price}  ({entry.reason})"
        )


if __name__ == "__main__":
    demo_backtest()
