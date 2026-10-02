"""Smoke test for the actual Streamlit page, via Streamlit's own
`AppTest` framework — loads the real `dashboard.py` (demo mode, so no
network/credentials) and asserts it renders without raising.

The data-fetching logic itself has its own thorough tests in
test_dashboard_data.py; this just confirms the UI wiring doesn't crash.
"""
from pathlib import Path

from streamlit.testing.v1 import AppTest

DASHBOARD_PATH = Path(__file__).parent.parent / "src" / "vera_quant" / "dashboard.py"


def test_dashboard_loads_in_demo_mode_without_error() -> None:
    at = AppTest.from_file(str(DASHBOARD_PATH))
    at.run(timeout=60)
    assert not at.exception


def test_dashboard_shows_the_title() -> None:
    at = AppTest.from_file(str(DASHBOARD_PATH))
    at.run(timeout=60)
    titles = [t.value for t in at.title]
    assert any("Vera Quant" in title for title in titles)


def test_dashboard_shows_a_realized_pnl_metric() -> None:
    at = AppTest.from_file(str(DASHBOARD_PATH))
    at.run(timeout=60)
    metric_labels = [m.label for m in at.metric]
    assert "Realized P&L" in metric_labels


def test_dashboard_sidebar_shows_regime_state() -> None:
    at = AppTest.from_file(str(DASHBOARD_PATH))
    at.run(timeout=60)
    sidebar_metric_labels = [m.label for m in at.sidebar.metric]
    assert "State" in sidebar_metric_labels
