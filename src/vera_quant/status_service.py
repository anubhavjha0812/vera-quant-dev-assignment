"""A small FastAPI status service: health, positions, P&L, regime,
kill-switch state. Internal-dashboard use (the email's good-to-have);
kept to what's cheaply unit-testable without binding a real server (see
test_status_service.py, driven entirely through FastAPI's TestClient).

The Streamlit dashboard and Grafana/docker-compose this would sit behind
are deferred to step 17 if time allows — see DECISIONS.md #21.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI


@dataclass
class StatusState:
    """The live snapshot the endpoints read from. The live/paper runner
    (step 14) mutates this object in place as it processes bars; nothing
    in this module computes anything itself — it's a read-only view.
    """

    positions: dict[str, dict[str, str]] = field(default_factory=dict)
    realized_pnl: str = "0"
    regime_state: str = "NORMAL"
    tripped_kill_switches: list[str] = field(default_factory=list)
    feed_is_stale: bool = False


def create_app(state: StatusState) -> FastAPI:
    app = FastAPI(title="Vera Quant Status")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/positions")
    def positions() -> dict[str, dict[str, str]]:
        return state.positions

    @app.get("/pnl")
    def pnl() -> dict[str, str]:
        return {"realized_pnl": state.realized_pnl}

    @app.get("/regime")
    def regime() -> dict[str, str]:
        return {"state": state.regime_state}

    @app.get("/kill-switches")
    def kill_switches() -> dict[str, object]:
        return {"tripped": state.tripped_kill_switches, "feed_is_stale": state.feed_is_stale}

    return app
