"""dashboard_data.py's fetch functions are plain, testable Python — no
Streamlit involved — exercised here against a real FastAPI app via
`TestClient` (a sync, httpx-compatible wrapper around an in-process ASGI
app, no real server/port needed). The exact same functions run against a
real `httpx.Client(base_url=...)` in live use.
"""
from decimal import Decimal

from fastapi.testclient import TestClient

from vera_quant.dashboard_data import (
    fetch_blotter,
    fetch_kill_switches,
    fetch_open_orders,
    fetch_pnl,
    fetch_pnl_curve,
    fetch_positions,
    fetch_regime,
)
from vera_quant.status_service import StatusState, create_app


def _client() -> TestClient:
    state = StatusState(
        positions={"26000": {"net_quantity": "10", "avg_price": "100.50"}},
        realized_pnl="1234.56",
        regime_state="STRESSED",
        tripped_kill_switches=["stale_data"],
        feed_is_stale=True,
        blotter=[
            {"timestamp": "2026-01-02T09:20:00", "symbol": "NIFTY25DECFUT", "side": "BUY",
             "quantity": "25", "price": "100.50", "fees": "23.60"},
        ],
        open_orders=[
            {"idempotency_key": "abc123", "symbol": "NIFTY25DECFUT", "side": "SELL",
             "quantity": "25", "status": "OPEN"},
        ],
        pnl_curve=[
            {"timestamp": "2026-01-02T09:15:00", "realized_pnl": "0"},
            {"timestamp": "2026-01-02T09:20:00", "realized_pnl": "1234.56"},
        ],
    )
    app = create_app(state)
    return TestClient(app)


def test_fetch_positions() -> None:
    positions = fetch_positions(_client())
    assert positions["26000"]["net_quantity"] == "10"


def test_fetch_pnl_returns_decimal() -> None:
    pnl = fetch_pnl(_client())
    assert pnl == Decimal("1234.56")
    assert isinstance(pnl, Decimal)


def test_fetch_regime() -> None:
    assert fetch_regime(_client()) == "STRESSED"


def test_fetch_kill_switches() -> None:
    tripped, stale = fetch_kill_switches(_client())
    assert tripped == ["stale_data"]
    assert stale is True


def test_fetch_blotter_returns_rows() -> None:
    rows = fetch_blotter(_client())
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NIFTY25DECFUT"


def test_fetch_open_orders_returns_rows() -> None:
    rows = fetch_open_orders(_client())
    assert len(rows) == 1
    assert rows[0]["status"] == "OPEN"


def test_fetch_pnl_curve_returns_rows_in_order() -> None:
    rows = fetch_pnl_curve(_client())
    assert len(rows) == 2
    assert rows[-1]["realized_pnl"] == "1234.56"


def test_fetch_against_an_empty_state_returns_empty_collections() -> None:
    client = TestClient(create_app(StatusState()))
    assert fetch_positions(client) == {}
    assert fetch_blotter(client) == []
    assert fetch_open_orders(client) == []
    assert fetch_pnl_curve(client) == []
