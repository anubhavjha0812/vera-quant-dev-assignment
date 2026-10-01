"""Step 12's Done-when: mocked tests cover a timeout followed by a
duplicate check, a burst of 403s, and an expired session; a separate test
confirms placeOrder is never reached while BROKER_MODE=paper.
"""
from decimal import Decimal

from vera_quant.brokers.live_smartapi.broker import LiveSafetyLimits, SmartApiBroker
from vera_quant.brokers.live_smartapi.client import RateLimitError, SessionExpiredError
from vera_quant.models import Instrument, Order, OrderStatus, OrderType, TransactionType
from vera_quant.rate_limiter import RateLimiter


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


def _order(key: str = "k1", qty: int = 25) -> Order:
    return Order(
        idempotency_key=key,
        instrument=_instrument(),
        transaction_type=TransactionType.BUY,
        quantity=qty,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
    )


class FakeClient:
    def __init__(self) -> None:
        self.place_order_calls: list[dict[str, str]] = []
        self.generate_session_calls = 0
        self._place_order_queue: list[object] = []
        self._order_book_rows: list[dict[str, str]] = []

    def queue_place_order(self, outcome: object) -> None:
        """`outcome` is an Exception instance to raise, or a dict to return."""
        self._place_order_queue.append(outcome)

    def set_order_book(self, rows: list[dict[str, str]]) -> None:
        self._order_book_rows = rows

    def generate_session(self, client_code: str, password: str, totp: str) -> dict[str, object]:
        self.generate_session_calls += 1
        return {"status": True}

    def generate_tokens(self, refresh_token: str) -> dict[str, object]:
        return {"status": True}

    def place_order(self, params: dict[str, str]) -> dict[str, object]:
        self.place_order_calls.append(params)
        outcome = self._place_order_queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, dict)
        return outcome

    def get_order_book(self) -> dict[str, object]:
        return {"status": True, "data": self._order_book_rows}

    def get_margin(self, params: dict[str, object]) -> dict[str, object]:
        return {"status": True, "data": {"availablecash": "1000000"}}


def _rate_limiters() -> dict[str, RateLimiter]:
    return {
        "place_modify_cancel": RateLimiter(max_requests=9, window_seconds=1.0),
        "order_book": RateLimiter(max_requests=1, window_seconds=1.0),
        "margin": RateLimiter(max_requests=10, window_seconds=1.0),
    }


def _broker(client: FakeClient, broker_mode: str = "live") -> SmartApiBroker:
    return SmartApiBroker(
        client=client,
        broker_mode=broker_mode,
        live_limits=LiveSafetyLimits(max_order_size=1000, max_daily_orders=1000),
        rate_limiters=_rate_limiters(),
        sleep=lambda _s: None,
        retry_delay_seconds=0.001,
    )


# ------------------------------------------------------------ live arming


def test_place_order_is_never_reached_while_broker_mode_is_paper() -> None:
    client = FakeClient()
    broker = _broker(client, broker_mode="paper")
    broker_order_id, status = broker.submit_order(_order())
    assert len(client.place_order_calls) == 0
    assert status == OrderStatus.REJECTED
    assert broker_order_id is None


def test_live_mode_rejects_orders_above_the_safety_max_order_size() -> None:
    client = FakeClient()
    broker = SmartApiBroker(
        client=client,
        broker_mode="live",
        live_limits=LiveSafetyLimits(max_order_size=10, max_daily_orders=1000),
        rate_limiters=_rate_limiters(),
        sleep=lambda _s: None,
    )
    _, status = broker.submit_order(_order(qty=50))
    assert status == OrderStatus.REJECTED
    assert len(client.place_order_calls) == 0


def test_live_mode_rejects_orders_once_daily_count_ceiling_is_hit() -> None:
    client = FakeClient()
    broker = SmartApiBroker(
        client=client,
        broker_mode="live",
        live_limits=LiveSafetyLimits(max_order_size=1000, max_daily_orders=1),
        rate_limiters=_rate_limiters(),
        sleep=lambda _s: None,
    )
    client.queue_place_order({"status": True, "data": {"orderid": "O1"}})
    broker.submit_order(_order(key="k1"))  # consumes the one allowed order

    _, status = broker.submit_order(_order(key="k2"))
    assert status == OrderStatus.REJECTED
    assert len(client.place_order_calls) == 1  # the second never reached the client


# --------------------------------------------------------------- happy path


def test_live_order_places_successfully() -> None:
    client = FakeClient()
    broker = _broker(client)
    client.queue_place_order({"status": True, "data": {"orderid": "O123"}})
    broker_order_id, status = broker.submit_order(_order())
    assert broker_order_id == "O123"
    assert status == OrderStatus.OPEN
    assert len(client.place_order_calls) == 1


# ---------------------------------------------------------- timeout + dedup


def test_timeout_then_duplicate_check_finds_the_order_already_placed() -> None:
    client = FakeClient()
    broker = _broker(client)
    client.queue_place_order(TimeoutError("broker did not respond"))
    client.set_order_book([{"ordertag": "k1", "orderid": "O999", "orderstatus": "open"}])

    broker_order_id, status = broker.submit_order(_order(key="k1"))
    assert broker_order_id == "O999"
    assert status == OrderStatus.OPEN
    assert len(client.place_order_calls) == 1  # never blindly resent


def test_timeout_with_no_matching_order_in_book_retries_and_then_succeeds() -> None:
    client = FakeClient()
    broker = _broker(client)
    client.queue_place_order(TimeoutError("broker did not respond"))
    client.set_order_book([])  # nothing found -> safe to actually retry
    client.queue_place_order({"status": True, "data": {"orderid": "O1"}})

    broker_order_id, status = broker.submit_order(_order())
    assert broker_order_id == "O1"
    assert status == OrderStatus.OPEN
    assert len(client.place_order_calls) == 2


# ------------------------------------------------------------- 403 burst


def test_burst_of_403s_backs_off_without_relogin_then_succeeds() -> None:
    client = FakeClient()
    broker = _broker(client)
    client.queue_place_order(RateLimitError("403"))
    client.queue_place_order(RateLimitError("403"))
    client.queue_place_order({"status": True, "data": {"orderid": "O1"}})

    broker_order_id, status = broker.submit_order(_order())
    assert broker_order_id == "O1"
    assert status == OrderStatus.OPEN
    assert client.generate_session_calls == 0  # a 403 NEVER triggers re-login


# ----------------------------------------------------------- expired session


def test_expired_session_triggers_relogin_then_succeeds() -> None:
    client = FakeClient()
    broker = _broker(client)
    client.queue_place_order(SessionExpiredError("token invalid"))
    client.queue_place_order({"status": True, "data": {"orderid": "O1"}})

    broker_order_id, status = broker.submit_order(_order())
    assert broker_order_id == "O1"
    assert status == OrderStatus.OPEN
    assert client.generate_session_calls == 1


def test_exhausting_all_retries_results_in_rejected() -> None:
    client = FakeClient()
    broker = _broker(client)
    for _ in range(5):
        client.queue_place_order(RateLimitError("403"))
    _, status = broker.submit_order(_order())
    assert status == OrderStatus.REJECTED


# -------------------------------------------------------------------- margin


def test_margin_check_blocks_an_order_when_available_cash_is_insufficient() -> None:
    class LowMarginClient(FakeClient):
        def get_margin(self, params: dict[str, object]) -> dict[str, object]:
            return {"status": True, "data": {"availablecash": "1"}}

    low_margin_client = LowMarginClient()
    low_margin_client.queue_place_order({"status": True, "data": {"orderid": "O1"}})
    broker = SmartApiBroker(
        client=low_margin_client,
        broker_mode="live",
        live_limits=LiveSafetyLimits(max_order_size=1000, max_daily_orders=1000),
        rate_limiters=_rate_limiters(),
        sleep=lambda _s: None,
        required_margin_per_order=Decimal("5000"),
    )
    _, status = broker.submit_order(_order())
    assert status == OrderStatus.REJECTED
    assert len(low_margin_client.place_order_calls) == 0
