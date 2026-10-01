from fastapi.testclient import TestClient

from vera_quant.status_service import StatusState, create_app


def _state() -> StatusState:
    return StatusState(
        positions={"26000": {"net_quantity": "10", "avg_price": "100.50"}},
        realized_pnl="1234.56",
        regime_state="STRESSED",
        tripped_kill_switches=["stale_data"],
        feed_is_stale=True,
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


def test_endpoints_reflect_state_mutated_after_app_creation() -> None:
    """The live/paper runner (step 14) mutates this shared StatusState
    object as it processes bars — the app must read it live, not a frozen
    snapshot taken at create_app() time."""
    state = StatusState()
    client = TestClient(create_app(state))
    assert client.get("/regime").json() == {"state": "NORMAL"}

    state.regime_state = "CRISIS"
    assert client.get("/regime").json() == {"state": "CRISIS"}
