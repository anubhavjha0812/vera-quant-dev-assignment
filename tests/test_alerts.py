from decimal import Decimal

from vera_quant.alerts import Alert, NtfyAlertSender, dispatch_alerts, evaluate_alerts


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)


def _no_alert_conditions() -> dict:
    return dict(
        local_positions={"26000": 10},
        broker_positions={"26000": 10},
        expected_pnl=Decimal("100"),
        actual_pnl=Decimal("100"),
        pnl_drift_threshold=Decimal("50"),
        tripped_kill_switches=set(),
        feed_is_stale=False,
        recent_reject_count=0,
        reject_alert_threshold=3,
    )


def test_no_alerts_when_everything_is_fine() -> None:
    assert evaluate_alerts(**_no_alert_conditions()) == []


def test_position_mismatch_produces_an_alert() -> None:
    conditions = _no_alert_conditions()
    conditions["broker_positions"] = {"26000": 8}  # forced mismatch
    alerts = evaluate_alerts(**conditions)
    assert len(alerts) == 1
    assert alerts[0].category == "position_mismatch"
    assert "26000" in alerts[0].message


def test_pnl_drift_beyond_threshold_produces_an_alert() -> None:
    conditions = _no_alert_conditions()
    conditions["actual_pnl"] = Decimal("0")  # drift of 100, threshold 50
    alerts = evaluate_alerts(**conditions)
    assert any(a.category == "pnl_drift" for a in alerts)


def test_pnl_drift_within_threshold_produces_no_alert() -> None:
    conditions = _no_alert_conditions()
    conditions["actual_pnl"] = Decimal("90")  # drift of 10, threshold 50
    assert evaluate_alerts(**conditions) == []


def test_each_tripped_kill_switch_produces_its_own_alert() -> None:
    conditions = _no_alert_conditions()
    conditions["tripped_kill_switches"] = {"manual", "stale_data"}
    alerts = evaluate_alerts(**conditions)
    kill_switch_alerts = [a for a in alerts if a.category == "kill_switch"]
    assert len(kill_switch_alerts) == 2


def test_stale_feed_produces_an_alert() -> None:
    conditions = _no_alert_conditions()
    conditions["feed_is_stale"] = True
    alerts = evaluate_alerts(**conditions)
    assert any(a.category == "stale_feed" for a in alerts)


def test_reject_count_at_threshold_produces_an_alert() -> None:
    conditions = _no_alert_conditions()
    conditions["recent_reject_count"] = 3
    conditions["reject_alert_threshold"] = 3
    alerts = evaluate_alerts(**conditions)
    assert any(a.category == "rejects" for a in alerts)


def test_a_forced_mismatch_pings_the_phone() -> None:
    """Step 16's Done-when, literally: force a mismatch, confirm the
    alert sender actually gets the message (standing in for the phone)."""
    conditions = _no_alert_conditions()
    conditions["broker_positions"] = {"26000": 999}
    alerts = evaluate_alerts(**conditions)
    sender = FakeSender()
    dispatch_alerts(alerts, sender)
    assert len(sender.sent) == 1
    assert "position_mismatch" in sender.sent[0]


def test_ntfy_sender_posts_the_message_to_its_topic_url() -> None:
    posted = []

    def fake_post(url: str, body: str) -> None:
        posted.append((url, body))

    sender = NtfyAlertSender(topic_url="https://ntfy.sh/my-topic", post=fake_post)
    sender.send("hello")
    assert posted == [("https://ntfy.sh/my-topic", "hello")]


def test_dispatch_alerts_sends_one_message_per_alert() -> None:
    sender = FakeSender()
    alerts = [
        Alert(category="a", message="first"),
        Alert(category="b", message="second"),
    ]
    dispatch_alerts(alerts, sender)
    assert len(sender.sent) == 2
