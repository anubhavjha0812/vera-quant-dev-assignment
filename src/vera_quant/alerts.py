"""Phone alerting — exactly what the email asks for: "alerting to phone
the moment the engine deviates from expectation," on position mismatch,
P&L drift, kill-switch/breaker trips, stale feed, and rejects.

Default transport is ntfy (a plain HTTP POST to a topic URL) rather than a
Telegram bot — no bot token/chat-id setup needed, and `AlertSender` is a
Protocol so swapping in Telegram later is a new small class, not a
rewrite. `post` is injected everywhere so nothing here ever makes a real
network call in tests — see `evaluate_alerts`'s own test for the step 16
Done-when ("a forced mismatch pings your phone"), proven via a fake sender
capturing what would have been sent.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class AlertSender(Protocol):
    def send(self, message: str) -> None: ...


@dataclass
class NtfyAlertSender:
    topic_url: str
    post: Callable[[str, str], None]  # (url, body) -> None, injected for testability

    def send(self, message: str) -> None:
        self.post(self.topic_url, message)


@dataclass(frozen=True)
class Alert:
    category: str
    message: str


def evaluate_alerts(
    *,
    local_positions: dict[str, int],
    broker_positions: dict[str, int],
    expected_pnl: Decimal,
    actual_pnl: Decimal,
    pnl_drift_threshold: Decimal,
    tripped_kill_switches: set[str],
    feed_is_stale: bool,
    recent_reject_count: int,
    reject_alert_threshold: int,
) -> list[Alert]:
    """Pure function: given a snapshot of local vs broker-reported state,
    returns every condition worth phoning about. No side effects — see
    `dispatch_alerts` for actually sending them.
    """
    alerts: list[Alert] = []

    for token in sorted(set(local_positions) | set(broker_positions)):
        local_qty = local_positions.get(token, 0)
        broker_qty = broker_positions.get(token, 0)
        if local_qty != broker_qty:
            message = f"Position mismatch for {token}: local={local_qty}, broker={broker_qty}"
            alerts.append(Alert(category="position_mismatch", message=message))

    drift = abs(expected_pnl - actual_pnl)
    if drift > pnl_drift_threshold:
        alerts.append(
            Alert(
                category="pnl_drift",
                message=f"P&L drift of {drift} exceeds threshold {pnl_drift_threshold}",
            )
        )

    for switch in sorted(tripped_kill_switches):
        alerts.append(Alert(category="kill_switch", message=f"Kill switch tripped: {switch}"))

    if feed_is_stale:
        alerts.append(Alert(category="stale_feed", message="Market data feed is stale"))

    if recent_reject_count >= reject_alert_threshold:
        alerts.append(
            Alert(
                category="rejects",
                message=f"{recent_reject_count} order rejects in the recent window",
            )
        )

    return alerts


def dispatch_alerts(alerts: list[Alert], sender: AlertSender) -> None:
    for alert in alerts:
        sender.send(f"[{alert.category}] {alert.message}")
