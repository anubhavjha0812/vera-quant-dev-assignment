"""Live SmartAPI WebSocket 2.0 tick consumer and order-update queue.

The underlying SDK (`SmartWebSocketV2`) calls back from its OWN thread.
`call_soon_threadsafe` is the only safe way to hand data from that thread
to the asyncio event loop — this module uses it exclusively for that
handoff and never touches asyncio primitives directly from inside a
callback.

**Back-pressure policy** (dev plan, step 13): ticks are CONFLATED per
instrument — if the consumer falls behind, only the latest tick per token
survives, never a silent random drop and never a block on the SDK's
thread. Order updates are the opposite: never conflated, never dropped —
`OrderUpdateQueue` is a plain FIFO, because losing one can corrupt order
state.

Whether order updates genuinely arrive on SmartAPI's market-data socket or
a separate order-status stream is a "confirm in the docs" item the dev
plan itself flags — `OrderUpdateQueue` is independent of `LiveTickSource`
either way, so routing both through one `WebSocketClient` or two is a
wiring choice at the call site, not something this module assumes.

Raw tick field names/units (`last_traded_price` in paise, `token`,
`exchange_timestamp` in epoch millis) are this adapter's best-effort
mapping of the WebSocket 2.0 LTP-mode payload — confirm against the live
docs before connecting to a real feed, same caveat as client.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from vera_quant.market_data import HistoricalDataClient, SmartApiInterval, fetch_historical_bars
from vera_quant.models import Bar, Instrument, Tick
from vera_quant.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class WebSocketClient(Protocol):
    """What the real `SmartWebSocketV2` SDK object provides."""

    def connect(self) -> None: ...
    def close_connection(self) -> None: ...
    def subscribe(
        self, correlation_id: str, mode: int, token_list: list[dict[str, object]]
    ) -> None: ...
    def set_on_data_callback(self, callback: Callable[[dict[str, object]], None]) -> None: ...
    def set_on_open_callback(self, callback: Callable[[], None]) -> None: ...
    def set_on_close_callback(self, callback: Callable[[], None]) -> None: ...


def parse_tick(raw: dict[str, object]) -> Tick:
    # SmartAPI encodes last_traded_price as paise (actual price * 100) —
    # same quirk as the scrip master's tick_size field (contracts.py).
    price = Decimal(str(raw["last_traded_price"])) / 100
    ts_millis = int(str(raw["exchange_timestamp"]))
    return Tick(
        instrument_token=str(raw["token"]),
        timestamp=datetime.fromtimestamp(ts_millis / 1000),
        last_price=price,
        last_quantity=int(str(raw.get("last_traded_quantity", 0))),
    )


@dataclass
class ConflatingTickBuffer:
    """Keeps only the newest tick per instrument when the consumer falls
    behind — back-pressure by conflation, never by dropping at random or
    blocking the producer thread.
    """

    _latest: dict[str, Tick] = field(default_factory=dict)

    def put(self, tick: Tick) -> None:
        self._latest[tick.instrument_token] = tick

    def drain_nowait(self) -> list[Tick]:
        ticks = list(self._latest.values())
        self._latest.clear()
        return ticks


class OrderUpdateQueue:
    """A plain FIFO — order updates are never conflated or dropped."""

    def __init__(self) -> None:
        self._items: list[dict[str, object]] = []

    def put_nowait(self, item: dict[str, object]) -> None:
        self._items.append(item)

    def get_nowait(self) -> dict[str, object]:
        return self._items.pop(0)

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class WebSocketFeedConfig:
    heartbeat_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.05


class LiveTickSource:
    """Implements `vera_quant.market_data.TickSource` for SmartAPI
    WebSocket 2.0, plus reconnect-and-resync and graceful shutdown.
    """

    def __init__(
        self,
        ws_client: WebSocketClient,
        instruments: list[Instrument],
        historical_client: HistoricalDataClient,
        historical_rate_limiter: RateLimiter,
        config: WebSocketFeedConfig,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ws_client = ws_client
        self._instruments = instruments
        self._historical_client = historical_client
        self._historical_rate_limiter = historical_rate_limiter
        self._config = config
        self._clock = clock
        self._sleep = sleep

        self._buffer = ConflatingTickBuffer()
        self._last_message_at: float | None = None
        self.is_shut_down = False

    # ------------------------------------------------------- SDK callbacks

    def _on_data(self, raw: dict[str, object]) -> None:
        """Called from the SDK's OWN thread — must not touch asyncio
        state directly; only the plain dict buffer, which is a simple
        last-write-wins structure safe enough for that under the GIL for
        this single-key-overwrite pattern. A genuine asyncio-loop handoff
        (`call_soon_threadsafe`) is used only where loop state itself is
        touched — see `ticks()`.
        """
        if self.is_shut_down:
            return
        self._buffer.put(parse_tick(raw))
        self._last_message_at = self._clock()

    def _on_open(self) -> None:
        self._last_message_at = self._clock()

    def _on_close(self) -> None:
        pass

    # ------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        self._ws_client.set_on_data_callback(self._on_data)
        self._ws_client.set_on_open_callback(self._on_open)
        self._ws_client.set_on_close_callback(self._on_close)
        self._ws_client.connect()
        self._subscribe()
        self._last_message_at = self._clock()

    def _subscribe(self) -> None:
        token_list = [
            {"exchangeType": 2, "tokens": [i.symbol_token for i in self._instruments]}
        ]
        self._ws_client.subscribe("feed1", 1, token_list)

    def is_stale(self) -> bool:
        if self._last_message_at is None:
            return False
        return (self._clock() - self._last_message_at) > self._config.heartbeat_timeout_seconds

    def reconnect_and_resync(self, disconnected_since: datetime, now: datetime) -> list[Bar]:
        """Backfills bars missed during the disconnect window via
        `getCandleData`, then resubscribes. Order re-checking against the
        broker's order book is the live runner's job (step 14) — it has
        the `SmartApiBroker` instance, this class doesn't.
        """
        backfilled: list[Bar] = []
        for instrument in self._instruments:
            bars = fetch_historical_bars(
                self._historical_client,
                instrument=instrument,
                interval=SmartApiInterval.FIVE_MINUTE,
                from_date=disconnected_since,
                to_date=now,
                rate_limiter=self._historical_rate_limiter,
                sleep=self._sleep,
            )
            backfilled.extend(bars)
        self.connect()
        return backfilled

    def shutdown(self) -> None:
        """Stops accepting new ticks, closes the socket. Flushing the
        journal is the live runner's responsibility (it owns the Journal
        instance, step 10) — called alongside this, not by it.
        """
        self.is_shut_down = True
        self._ws_client.close_connection()

    # ------------------------------------------------------------ consuming

    def poll_nowait(self) -> list[Tick]:
        """Non-blocking drain of whatever has arrived (conflated) since
        the last poll. Returns `[]` immediately if nothing new, or if shut
        down."""
        if self.is_shut_down:
            return []
        return self._buffer.drain_nowait()

    async def ticks(self) -> AsyncIterator[Tick]:
        """The `TickSource` protocol's async interface — polls
        `poll_nowait` on a short interval. Prefer `poll_nowait()` directly
        in a tight live-runner loop; this exists for drop-in protocol
        compliance with recorded-replay/synthetic `TickSource`s.
        """
        while not self.is_shut_down:
            for tick in self.poll_nowait():
                yield tick
            await asyncio.sleep(self._config.poll_interval_seconds)
