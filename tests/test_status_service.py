from fastapi.testclient import TestClient

from vera_quant.status_service import StatusState, create_app


def _state() -> StatusState:
    return StatusState(
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


def test_health_endpoint() -> None:
    client = TestClient(create_app(StatusState()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_positions_endpoint_reflects_the_live_state() -> None:
    client = TestClient(create_app(_state()))
    response = client.get("/positions")
    assert response.status_code == 200
    assert response.json() == {"26000": {"net_quantity": "10", "avg_price": "100.50"}}


def test_pnl_endpoint() -> None:
    client = TestClient(create_app(_state()))
    response = client.get("/pnl")
    assert response.json() == {"realized_pnl": "1234.56"}


def test_regime_endpoint() -> None:
    client = TestClient(create_app(_state()))
    response = client.get("/regime")
    assert response.json() == {"state": "STRESSED"}


def test_kill_switches_endpoint() -> None:
    client = TestClient(create_app(_state()))
    response = client.get("/kill-switches")
    assert response.json() == {"tripped": ["stale_data"], "feed_is_stale": True}


def test_blotter_endpoint() -> None:
    client = TestClient(create_app(_state()))
    response = client.get("/blotter")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["symbol"] == "NIFTY25DECFUT"


def test_open_orders_endpoint() -> None:
    client = TestClient(create_app(_state()))
    response = client.get("/open-orders")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["status"] == "OPEN"


def test_pnl_curve_endpoint() -> None:
    client = TestClient(create_app(_state()))
    response = client.get("/pnl-curve")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[-1]["realized_pnl"] == "1234.56"


def test_blotter_and_open_orders_default_to_empty() -> None:
    client = TestClient(create_app(StatusState()))
    assert client.get("/blotter").json() == []
    assert client.get("/open-orders").json() == []
    assert client.get("/pnl-curve").json() == []


def test_endpoints_reflect_state_mutated_after_app_creation() -> None:
    """The live/paper runner (step 14) mutates this shared StatusState
    object as it processes bars — the app must read it live, not a frozen
    snapshot taken at create_app() time."""
    state = StatusState()
    client = TestClient(create_app(state))
    assert client.get("/regime").json() == {"state": "NORMAL"}

    state.regime_state = "CRISIS"
    assert client.get("/regime").json() == {"state": "CRISIS"}
