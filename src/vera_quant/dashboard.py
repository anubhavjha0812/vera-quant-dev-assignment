"""Streamlit dashboard — sits on top of `status_service.py`'s FastAPI
endpoints via `dashboard_data.py`'s fetch functions (same code path for
demo and live use, just a different HTTP client underneath).

Run: `poetry run streamlit run src/vera_quant/dashboard.py`

**Demo mode (default)**: runs a synthetic backtest in-process, wraps the
result in a FastAPI app via `status_service.create_app()`, and queries it
through `TestClient` — no real server/port needed, no network, no
credentials, the same one-command spirit as `poetry run demo-backtest`.

**Live mode**: set `STATUS_SERVICE_URL` (e.g. `http://localhost:8000`) to
point at a real running status service instead — `dashboard_data.py`'s
functions don't care which; they take any `httpx.Client`.
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal

import httpx
import pandas as pd
import streamlit as st
from fastapi.testclient import TestClient

from vera_quant.backtest import BacktestResult, run_backtest
from vera_quant.brokers.paper import PaperBroker
from vera_quant.costs import CostRateSchedule
from vera_quant.dashboard_data import (
    fetch_blotter,
    fetch_kill_switches,
    fetch_open_orders,
    fetch_pnl,
    fetch_pnl_curve,
    fetch_positions,
    fetch_regime,
)
from vera_quant.indicators import Atr
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Bar, Instrument, OrderIntent, Position
from vera_quant.risk import RiskParams, RiskState, risk_gate
from vera_quant.status_service import StatusState, create_app
from vera_quant.strategies import SarParams, SarState, sar_step


def run_demo_backtest() -> BacktestResult:
    """The same wiring as `cli.demo_backtest()` — kept separate so the
    dashboard can reuse it without printing to stdout."""
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
    return run_backtest(bars, instrument, decide, risk_check, broker)


def status_state_from_backtest(result: BacktestResult) -> StatusState:
    """Shapes a `BacktestResult` into the same `StatusState` a live/paper
    runner would maintain — so the dashboard's demo mode exercises the
    identical rendering code path a real deployment would use."""
    assert result.final_position is not None
    position = result.final_position

    positions: dict[str, dict[str, str]] = {}
    if position.net_quantity != 0:
        positions[position.instrument.symbol_token] = {
            "net_quantity": str(position.net_quantity),
            "avg_price": str(position.avg_price),
        }

    blotter = [
        {
            "timestamp": entry.timestamp.isoformat(),
            "symbol": entry.trading_symbol,
            "side": entry.transaction_type,
            "quantity": str(entry.quantity),
            "price": str(entry.fill_price),
            "fees": str(entry.fees),
        }
        for entry in result.blotter
    ]

    pnl_curve = [
        {"timestamp": str(i), "realized_pnl": str(pnl)}
        for i, pnl in enumerate(result.equity_curve)
    ]

    return StatusState(
        positions=positions,
        realized_pnl=str(position.realized_pnl),
        regime_state="NORMAL",
        tripped_kill_switches=[],
        feed_is_stale=False,
        blotter=blotter,
        open_orders=[],  # a backtest run has nothing left "open" to show
        pnl_curve=pnl_curve,
    )


def get_client() -> httpx.Client:
    """Live mode if `STATUS_SERVICE_URL` is set, else an in-process demo
    backtest wrapped in the same FastAPI app `status_service.py` defines.
    """
    status_url = os.environ.get("STATUS_SERVICE_URL")
    if status_url:
        return httpx.Client(base_url=status_url)
    state = status_state_from_backtest(run_demo_backtest())
    return TestClient(create_app(state))


def render(client: httpx.Client) -> None:
    st.set_page_config(page_title="Vera Quant Status", layout="wide")
    st.title("Vera Quant — Status Dashboard")

    regime = fetch_regime(client)
    tripped, feed_stale = fetch_kill_switches(client)

    with st.sidebar:
        st.subheader("Regime")
        st.metric("State", regime)
        st.subheader("Kill switches")
        if tripped:
            for switch in tripped:
                st.error(f"TRIPPED: {switch}")
        else:
            st.success("None tripped")
        if feed_stale:
            st.warning("Feed is stale")

    realized_pnl = fetch_pnl(client)
    st.metric("Realized P&L", f"{realized_pnl:,.2f}")

    positions = fetch_positions(client)
    st.subheader("Positions")
    if positions:
        st.dataframe(pd.DataFrame.from_dict(positions, orient="index"))
    else:
        st.caption("No open positions")

    pnl_curve = fetch_pnl_curve(client)
    st.subheader("P&L curve")
    if pnl_curve:
        curve_df = pd.DataFrame(pnl_curve)
        curve_df["realized_pnl"] = curve_df["realized_pnl"].astype(float)
        st.line_chart(curve_df.set_index("timestamp")["realized_pnl"])
    else:
        st.caption("No P&L history yet")

    open_orders = fetch_open_orders(client)
    st.subheader("Open orders")
    if open_orders:
        st.dataframe(pd.DataFrame(open_orders))
    else:
        st.caption("No open orders")

    blotter = fetch_blotter(client)
    st.subheader("Trade blotter")
    if blotter:
        st.dataframe(pd.DataFrame(blotter))
    else:
        st.caption("No trades yet")


render(get_client())
