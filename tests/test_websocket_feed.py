"""Step 13's Done-when: a fake feed that disconnects mid-stream produces
the same bars as an uninterrupted run.
"""
import asyncio
import threading
from datetime import datetime, timedelta
from decimal import Decimal

from vera_quant.brokers.live_smartapi.websocket_feed import (
    ConflatingTickBuffer,
    LiveTickSource,
    OrderUpdateQueue,
    WebSocketFeedConfig,
    parse_tick,
)
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Bar, Instrument, Tick
from vera_quant.rate_limiter import RateLimiter


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


class FakeWebSocketClient:
    """Stands in for the real SmartWebSocketV2 SDK object."""

    def __init__(self) -> None:
        self.connected = False
        self.subscriptions: list[tuple[str, int, list[dict[str, object]]]] = []
        self.closed = False
        self._on_data: object = None
        self._on_open: object = None
        self._on_close: object = None

    def set_on_data_callback(self, callback) -> None:  # type: ignore[no-untyped-def]
        self._on_data = callback

    def set_on_open_callback(self, callback) -> None:  # type: ignore[no-untyped-def]
        self._on_open = callback

    def set_on_close_callback(self, callback) -> None:  # type: ignore[no-untyped-def]
        self._on_close = callback

    def connect(self) -> None:
        self.connected = True
        if self._on_open:
            self._on_open()  # type: ignore[misc]

    def close_connection(self) -> None:
        self.connected = False
        self.closed = True
        if self._on_close:
            self._on_close()  # type: ignore[misc]

    def subscribe(self, correlation_id: str, mode: int, token_list) -> None:  # type: ignore[no-untyped-def]
        self.subscriptions.append((correlation_id, mode, token_list))

    def push_raw(self, raw: dict[str, object]) -> None:
        """Simulates the SDK calling back with a tick — from whatever
        thread calls this."""
        if self._on_data:
            self._on_data(raw)  # type: ignore[misc]


def _raw_tick(token: str, price: Decimal, ts: datetime, qty: int = 10) -> dict[str, object]:
    return {
        "token": token,
        "last_traded_price": str(int(price * 100)),  # SmartAPI paise-unit quirk
        "exchange_timestamp": str(int(ts.timestamp() * 1000)),
        "last_traded_quantity": str(qty),
    }


class FakeHistoricalClient:
    """Returns bars for the backfill window — the data that would have
    arrived via ticks had the connection not dropped."""

    def __init__(self, bars_by_window: list[Bar]) -> None:
        self._bars = bars_by_window

    def get_candle_data(self, params: dict[str, str]) -> dict[str, object]:
        rows = [
            [
                b.timestamp.isoformat(),
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                str(b.volume),
            ]
            for b in self._bars
        ]
        return {"status": True, "data": rows}


# ------------------------------------------------------------------ parsing


def test_parse_tick_converts_paise_units_and_epoch_millis() -> None:
    ts = datetime(2026, 1, 2, 9, 15, 30)
    raw = _raw_tick("26000", Decimal("100.50"), ts, qty=7)
    tick = parse_tick(raw)
    assert tick.instrument_token == "26000"
    assert tick.last_price == Decimal("100.50")
    assert tick.last_quantity == 7
    assert abs((tick.timestamp - ts).total_seconds()) < 1


# -------------------------------------------------------------- conflation


def test_conflating_buffer_keeps_only_the_latest_tick_per_instrument() -> None:
    buf = ConflatingTickBuffer()
    ts = datetime(2026, 1, 2, 9, 15)
    buf.put(Tick(instrument_token="A", timestamp=ts, last_price=Decimal("100"), last_quantity=1))
    buf.put(Tick(instrument_token="A", timestamp=ts, last_price=Decimal("101"), last_quantity=1))
    buf.put(Tick(instrument_token="B", timestamp=ts, last_price=Decimal("50"), last_quantity=1))

    drained = buf.drain_nowait()
    by_token = {t.instrument_token: t for t in drained}
    assert len(drained) == 2
    assert by_token["A"].last_price == Decimal("101")  # the later one won, not both queued


def test_conflating_buffer_drain_nowait_is_empty_when_nothing_pushed() -> None:
    buf = ConflatingTickBuffer()
    assert buf.drain_nowait() == []


# --------------------------------------------------------- order updates


def test_order_update_queue_never_drops_even_under_backpressure() -> None:
    queue = OrderUpdateQueue()
    for i in range(50):
        queue.put_nowait({"orderid": str(i)})
    drained = [queue.get_nowait() for _ in range(50)]
    assert len(drained) == 50
    assert [d["orderid"] for d in drained] == [str(i) for i in range(50)]  # FIFO, nothing lost


# ------------------------------------------------------------------ heartbeat


def test_feed_is_stale_after_heartbeat_timeout() -> None:
    clock = {"now": 0.0}
    source = LiveTickSource(
        ws_client=FakeWebSocketClient(),
        instruments=[_instrument()],
        historical_client=FakeHistoricalClient([]),
        historical_rate_limiter=RateLimiter(3, 1.0),
        config=WebSocketFeedConfig(heartbeat_timeout_seconds=10.0),
        clock=lambda: clock["now"],
        sleep=lambda _s: None,
    )
    source.connect()
    assert not source.is_stale()
    clock["now"] = 20.0
    assert source.is_stale()


# ------------------------------------------------------------- shutdown


def test_graceful_shutdown_closes_the_connection() -> None:
    ws_client = FakeWebSocketClient()
    source = LiveTickSource(
        ws_client=ws_client,
        instruments=[_instrument()],
        historical_client=FakeHistoricalClient([]),
        historical_rate_limiter=RateLimiter(3, 1.0),
        config=WebSocketFeedConfig(),
        sleep=lambda _s: None,
    )
    source.connect()
    source.shutdown()
    assert ws_client.closed
    assert source.is_shut_down


def test_shutdown_stops_further_ticks_from_being_buffered() -> None:
    ws_client = FakeWebSocketClient()
    source = LiveTickSource(
        ws_client=ws_client,
        instruments=[_instrument()],
        historical_client=FakeHistoricalClient([]),
        historical_rate_limiter=RateLimiter(3, 1.0),
        config=WebSocketFeedConfig(),
        sleep=lambda _s: None,
    )
    source.connect()
    source.shutdown()
    ws_client.push_raw(_raw_tick("26000", Decimal("100"), datetime(2026, 1, 2, 9, 15)))
    assert source.poll_nowait() == []


# ------------------------------------------------------ disconnect/resync


def _ticks_for_bar(bar: Bar, instrument_token: str) -> list[Tick]:
    """4 ticks per bar (open, high, low, close, in that order) so a
    BarAggregator fed these reconstructs the bar's EXACT OHLCV — needed so
    tick-derived bars are comparable to REST-backfilled bars at all; a
    single tick per bar would collapse O=H=L=C and could never match.
    """
    prices = [bar.open, bar.high, bar.low, bar.close]
    base_qty = bar.volume // 4
    quantities = [base_qty, base_qty, base_qty, bar.volume - 3 * base_qty]
    return [
        Tick(
            instrument_token=instrument_token,
            timestamp=bar.timestamp + timedelta(seconds=i * 10),
            last_price=price,
            last_quantity=qty,
        )
        for i, (price, qty) in enumerate(zip(prices, quantities, strict=True))
    ]


def test_disconnect_mid_stream_produces_the_same_bars_as_an_uninterrupted_run() -> None:
    instrument = _instrument()
    start = datetime(2026, 1, 2, 9, 15)
    bars = generate_synthetic_bars(
        instrument_token=instrument.symbol_token,
        start=start,
        periods=20,
        interval_minutes=5,
        pattern="chop",
        seed=50,
    )
    ticks = [t for b in bars for t in _ticks_for_bar(b, instrument.symbol_token)]

    from vera_quant.market_data import BarAggregator

    # --- Run A: uninterrupted ---
    ws_a = FakeWebSocketClient()
    source_a = LiveTickSource(
        ws_client=ws_a,
        instruments=[instrument],
        historical_client=FakeHistoricalClient([]),
        historical_rate_limiter=RateLimiter(3, 1.0),
        config=WebSocketFeedConfig(),
        sleep=lambda _s: None,
    )
    source_a.connect()
    agg_a = BarAggregator(instrument.symbol_token, interval_minutes=5)
    produced_a: list[Bar] = []
    for tick in ticks:
        ws_a.push_raw(
            _raw_tick(tick.instrument_token, tick.last_price, tick.timestamp, tick.last_quantity)
        )
        for t in source_a.poll_nowait():
            completed = agg_a.on_tick(t)
            if completed:
                produced_a.append(completed)
    final_a = agg_a.flush()
    if final_a:
        produced_a.append(final_a)

    # --- Run B: disconnects halfway, resyncs via backfill, then resumes ---
    midpoint = 10
    ws_b = FakeWebSocketClient()
    missed_bars = bars[:midpoint]  # what the backfill should recover
    source_b = LiveTickSource(
        ws_client=ws_b,
        instruments=[instrument],
        historical_client=FakeHistoricalClient(missed_bars),
        historical_rate_limiter=RateLimiter(3, 1.0),
        config=WebSocketFeedConfig(),
        sleep=lambda _s: None,
    )
    source_b.connect()
    agg_b = BarAggregator(instrument.symbol_token, interval_minutes=5)
    produced_b: list[Bar] = []

    # Connection drops before any live tick for this window was processed.
    ws_b.close_connection()

    backfilled = source_b.reconnect_and_resync(
        disconnected_since=start, now=bars[midpoint].timestamp
    )
    produced_b.extend(backfilled)

    remaining_ticks = [
        t for b in bars[midpoint:] for t in _ticks_for_bar(b, instrument.symbol_token)
    ]
    for tick in remaining_ticks:
        ws_b.push_raw(
            _raw_tick(tick.instrument_token, tick.last_price, tick.timestamp, tick.last_quantity)
        )
        for t in source_b.poll_nowait():
            completed = agg_b.on_tick(t)
            if completed:
                produced_b.append(completed)
    final_b = agg_b.flush()
    if final_b:
        produced_b.append(final_b)

    assert produced_b == produced_a


# --------------------------------------------------------- race condition


def test_concurrent_tick_hammering_with_shutdown_is_deterministic() -> None:
    """Runs the hammer-and-shutdown scenario several times; the final
    conflated-per-instrument state must be identical every time, because
    conflation always keeps whichever tick was pushed last in the fixed
    sequence — true regardless of thread-scheduling jitter.
    """
    instrument = _instrument()
    base_ts = datetime(2026, 1, 2, 9, 15, 0)
    fixed_sequence = [
        _raw_tick("26000", Decimal(str(100 + i)), base_ts + timedelta(seconds=i))
        for i in range(200)
    ]

    results = []
    for _ in range(5):
        ws_client = FakeWebSocketClient()
        source = LiveTickSource(
            ws_client=ws_client,
            instruments=[instrument],
            historical_client=FakeHistoricalClient([]),
            historical_rate_limiter=RateLimiter(3, 1.0),
            config=WebSocketFeedConfig(),
            sleep=lambda _s: None,
        )
        source.connect()

        def hammer(ws_client: FakeWebSocketClient = ws_client) -> None:
            for raw in fixed_sequence:
                ws_client.push_raw(raw)

        thread = threading.Thread(target=hammer)
        thread.start()
        thread.join(timeout=5)
        # Drain whatever arrived BEFORE shutting down — shutdown stops
        # accepting new ticks, it doesn't discard what's already buffered.
        drained = source.poll_nowait()
        source.shutdown()
        results.append(drained[0].last_price if drained else None)

    assert len(set(results)) == 1  # identical final state on every run
    assert results[0] == Decimal("299")  # the last tick in the fixed sequence


def test_live_tick_source_implements_the_ticksource_protocol() -> None:
    """Sanity check: async ticks() generator works for protocol
    compliance, draining whatever poll_nowait would have."""

    async def _drive() -> list[Tick]:
        ws_client = FakeWebSocketClient()
        source = LiveTickSource(
            ws_client=ws_client,
            instruments=[_instrument()],
            historical_client=FakeHistoricalClient([]),
            historical_rate_limiter=RateLimiter(3, 1.0),
            config=WebSocketFeedConfig(poll_interval_seconds=0.001),
            sleep=lambda _s: None,
        )
        source.connect()
        ws_client.push_raw(_raw_tick("26000", Decimal("100"), datetime(2026, 1, 2, 9, 15)))

        collected: list[Tick] = []
        async for tick in source.ticks():
            collected.append(tick)
            if len(collected) == 1:
                source.shutdown()
                break
        return collected

    collected = asyncio.run(_drive())
    assert len(collected) == 1
    assert collected[0].instrument_token == "26000"
