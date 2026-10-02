"""Thin, testable fetch functions between `dashboard.py` (Streamlit) and
`status_service.py` (FastAPI) — plain Python, no Streamlit import here, so
every function is exercised directly in tests via `httpx`'s ASGI
transport (no real server/port needed) and the exact same functions run
against a real URL in live use (`httpx.Client(base_url=...)`).
"""
from __future__ import annotations

from decimal import Decimal

import httpx


def fetch_positions(client: httpx.Client) -> dict[str, dict[str, str]]:
    response = client.get("/positions")
    response.raise_for_status()
    data: dict[str, dict[str, str]] = response.json()
    return data


def fetch_pnl(client: httpx.Client) -> Decimal:
    response = client.get("/pnl")
    response.raise_for_status()
    return Decimal(response.json()["realized_pnl"])


def fetch_regime(client: httpx.Client) -> str:
    response = client.get("/regime")
    response.raise_for_status()
    state: str = response.json()["state"]
    return state


def fetch_kill_switches(client: httpx.Client) -> tuple[list[str], bool]:
    response = client.get("/kill-switches")
    response.raise_for_status()
    data = response.json()
    return data["tripped"], data["feed_is_stale"]


def fetch_blotter(client: httpx.Client) -> list[dict[str, str]]:
    response = client.get("/blotter")
    response.raise_for_status()
    data: list[dict[str, str]] = response.json()
    return data


def fetch_open_orders(client: httpx.Client) -> list[dict[str, str]]:
    response = client.get("/open-orders")
    response.raise_for_status()
    data: list[dict[str, str]] = response.json()
    return data


def fetch_pnl_curve(client: httpx.Client) -> list[dict[str, str]]:
    response = client.get("/pnl-curve")
    response.raise_for_status()
    data: list[dict[str, str]] = response.json()
    return data
