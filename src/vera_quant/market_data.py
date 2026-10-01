"""Bar/tick storage, synthetic data, tick-to-bar aggregation, historical
download, and a pluggable TickSource interface.

Both "real" (downloaded/recorded) and synthetic bars are plain `list[Bar]`
— the same type backtest and live both consume, so there is exactly one
bar-iteration code path (CLAUDE.md rule 3). Money fields round-trip through
storage as strings, never float64 columns (CLAUDE.md rule 1).

The historical-download shape (`getCandleData` params, retry/backoff,
rate-limit detection, dropping an in-progress last candle) mirrors the
working pattern in `reference/01-v6.py`'s `get_historical_data` — the real
SmartAPI-backed client is wired in at step 12; here it's an injected
`HistoricalDataClient`, exercised against a fake in tests.
"""
from __future__ import annotations

import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Protocol

import pandas as pd

from vera_quant.contracts import select_active_contract
from vera_quant.models import Bar, Instrument, Tick
from vera_quant.rate_limiter import RateLimiter

# ------------------------------------------------------------------ storage

# Money fields are stored as strings (not float64) so Decimal precision
# survives the round trip — a float64 Parquet column would silently violate
# CLAUDE.md rule 1.


def write_bars_parquet(bars: list[Bar], path: Path) -> None:
    df = pd.DataFrame(
        {
            "instrument_token": [b.instrument_token for b in bars],
            "timestamp": [b.timestamp for b in bars],
            "open": [str(b.open) for b in bars],
            "high": [str(b.high) for b in bars],
            "low": [str(b.low) for b in bars],
            "close": [str(b.close) for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    df.to_parquet(path, index=False)


def read_bars_parquet(path: Path) -> list[Bar]:
    df = pd.read_parquet(path)
    return [
        Bar(
            instrument_token=str(row.instrument_token),
            timestamp=_to_datetime(row.timestamp),
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=int(str(row.volume)),
        )
        for row in df.itertuples(index=False)
    ]


def write_ticks_parquet(ticks: list[Tick], path: Path) -> None:
    df = pd.DataFrame(
        {
            "instrument_token": [t.instrument_token for t in ticks],
            "timestamp": [t.timestamp for t in ticks],
            "last_price": [str(t.last_price) for t in ticks],
            "last_quantity": [t.last_quantity for t in ticks],
        }
    )
    df.to_parquet(path, index=False)


def read_ticks_parquet(path: Path) -> list[Tick]:
    df = pd.read_parquet(path)
    return [
        Tick(
            instrument_token=str(row.instrument_token),
            timestamp=_to_datetime(row.timestamp),
            last_price=Decimal(str(row.last_price)),
            last_quantity=int(str(row.last_quantity)),
        )
        for row in df.itertuples(index=False)
    ]


def _to_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        result = to_pydatetime()
        assert isinstance(result, datetime)
        return result
    raise TypeError(f"cannot convert {value!r} to datetime")


# --------------------------------------------------------------- synthetic


def generate_synthetic_bars(
    *,
    instrument_token: str,
    start: datetime,
    periods: int,
    interval_minutes: int = 5,
    pattern: str = "trend_up",
    seed: int = 0,
    base_price: Decimal = Decimal("100.00"),
) -> list[Bar]:
    """Deterministic synthetic OHLCV bars for tests — same seed, same bars.

    `pattern`: "trend_up" | "trend_down" | "chop" | "gap" | "vol_spike".
    """
    valid_patterns = {"trend_up", "trend_down", "chop", "gap", "vol_spike"}
    if pattern not in valid_patterns:
        raise ValueError(f"unknown pattern {pattern!r}, expected one of {sorted(valid_patterns)}")

    rng = random.Random(seed)
    bars: list[Bar] = []
    price = base_price
    spike_width = max(3, periods // 5)
    spike_start, spike_end = periods // 3, periods // 3 + spike_width

    for i in range(periods):
        ts = start + timedelta(minutes=interval_minutes * i)

        if pattern == "trend_up":
            drift, noise_range = Decimal("0.30"), 0.15
        elif pattern == "trend_down":
            drift, noise_range = Decimal("-0.30"), 0.15
        elif pattern == "chop":
            drift, noise_range = Decimal("0.00"), 0.40
        elif pattern == "gap":
            drift = Decimal("5.00") if i == periods // 2 else Decimal("0.00")
            noise_range = 0.10
        else:  # vol_spike
            drift, noise_range = Decimal("0.00"), (2.0 if spike_start <= i < spike_end else 0.10)

        noise = Decimal(f"{rng.uniform(-noise_range, noise_range):.2f}")
        open_ = price
        close = max(open_ + drift + noise, Decimal("0.05"))
        high = max(open_, close) + Decimal(f"{abs(rng.uniform(0, 0.10)):.2f}")
        low = max(min(open_, close) - Decimal(f"{abs(rng.uniform(0, 0.10)):.2f}"), Decimal("0.01"))
        volume = rng.randint(100, 1000)

        bars.append(
            Bar(
                instrument_token=instrument_token,
                timestamp=ts,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
        price = close

    return bars


# --------------------------------------------------------- bar aggregator


@dataclass
class _WorkingBar:
    bucket_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class BarAggregator:
    """Incremental tick -> bar aggregator.

    One `on_tick()` call per tick; returns the just-closed `Bar` when a
    tick starts a new interval, else `None`. No DataFrame, no `iterrows()`
    — runs identically in backtest replay and live (CLAUDE.md rules 3, 9).
    """

    def __init__(self, instrument_token: str, interval_minutes: int) -> None:
        self._token = instrument_token
        self._interval_minutes = interval_minutes
        self._working: _WorkingBar | None = None

    def _bucket_for(self, ts: datetime) -> datetime:
        epoch = datetime(ts.year, ts.month, ts.day)
        minutes = (ts - epoch).total_seconds() / 60
        bucket_index = int(minutes // self._interval_minutes)
        return epoch + timedelta(minutes=bucket_index * self._interval_minutes)

    def on_tick(self, tick: Tick) -> Bar | None:
        bucket = self._bucket_for(tick.timestamp)
        completed: Bar | None = None

        if self._working is not None and bucket != self._working.bucket_start:
            completed = self._to_bar(self._working)
            self._working = None

        if self._working is None:
            self._working = _WorkingBar(
                bucket_start=bucket,
                open=tick.last_price,
                high=tick.last_price,
                low=tick.last_price,
                close=tick.last_price,
                volume=tick.last_quantity,
            )
        else:
            self._working.high = max(self._working.high, tick.last_price)
            self._working.low = min(self._working.low, tick.last_price)
            self._working.close = tick.last_price
            self._working.volume += tick.last_quantity

        return completed

    def _to_bar(self, w: _WorkingBar) -> Bar:
        return Bar(
            instrument_token=self._token,
            timestamp=w.bucket_start,
            open=w.open,
            high=w.high,
            low=w.low,
            close=w.close,
            volume=w.volume,
        )

    def flush(self) -> Bar | None:
        """Close out the in-progress bar at end of stream/session."""
        if self._working is None:
            return None
        bar = self._to_bar(self._working)
        self._working = None
        return bar


# -------------------------------------------------------------- TickSource


class TickSource(Protocol):
    """Uniform interface for anything that produces a stream of ticks —
    live SmartAPI WebSocket (step 13), a recorded replay, or a vendor feed
    (TrueData/GDFL-style). Async so every implementation shares one shape.
    """

    def ticks(self) -> AsyncIterator[Tick]: ...


class ParquetReplayTickSource:
    """Replays ticks previously recorded with `write_ticks_parquet` — the
    raw tick-level pipeline: record once, replay deterministically for
    backtests or to reproduce a live incident.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def ticks(self) -> AsyncIterator[Tick]:
        for tick in read_ticks_parquet(self._path):
            yield tick


# ------------------------------------------------------- continuous series


def stitch_continuous_series(
    bars_by_token: dict[str, list[Bar]],
    chain: list[Instrument],
    roll_buffer_days: int,
) -> list[Bar]:
    """Build one continuous bar series from a futures chain's per-contract
    bars, switching contracts per step 4's roll rule at each bar's date.

    `chain` must be sorted by expiry ascending (as
    `ScripMaster.futures_chain` returns it); each contract's bars must be
    sorted by timestamp ascending.
    """
    if not chain:
        raise ValueError("empty contract chain")

    by_token_by_ts = {
        token: {bar.timestamp: bar for bar in bars} for token, bars in bars_by_token.items()
    }
    all_timestamps = sorted({ts for ts_map in by_token_by_ts.values() for ts in ts_map})

    stitched: list[Bar] = []
    for ts in all_timestamps:
        active = select_active_contract(chain, as_of=ts.date(), roll_buffer_days=roll_buffer_days)
        bar = by_token_by_ts.get(active.symbol_token, {}).get(ts)
        if bar is not None:
            stitched.append(bar)
    return stitched


# ----------------------------------------------------------- historical DL


class SmartApiInterval(str, Enum):
    """Angel One SmartAPI's interval strings for `getCandleData`."""

    ONE_MINUTE = "ONE_MINUTE"
    THREE_MINUTE = "THREE_MINUTE"
    FIVE_MINUTE = "FIVE_MINUTE"
    TEN_MINUTE = "TEN_MINUTE"
    FIFTEEN_MINUTE = "FIFTEEN_MINUTE"
    THIRTY_MINUTE = "THIRTY_MINUTE"
    ONE_HOUR = "ONE_HOUR"
    ONE_DAY = "ONE_DAY"


class HistoricalDataClient(Protocol):
    """What step 12's real SmartAPI adapter implements; tests use a fake."""

    def get_candle_data(self, params: dict[str, str]) -> dict[str, object]: ...


def drop_incomplete_last_bar(bars: list[Bar], as_of: datetime) -> list[Bar]:
    """Angel One's candle endpoint can return an in-progress final candle
    for the current minute — drop it so bar-close-only logic (step 11's
    no-lookahead fill rule) never sees a bar that hasn't actually closed
    yet. Mirrors the `ommit` check in reference/01-v6.py.
    """
    if not bars:
        return bars
    last_minute = bars[-1].timestamp.replace(second=0, microsecond=0)
    current_minute = as_of.replace(second=0, microsecond=0)
    if last_minute == current_minute:
        return bars[:-1]
    return bars


def _row_to_bar(instrument_token: str, row: list[object]) -> Bar:
    ts_raw, o, h, lo, c, v = row
    return Bar(
        instrument_token=instrument_token,
        timestamp=datetime.fromisoformat(str(ts_raw)),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(lo)),
        close=Decimal(str(c)),
        volume=int(float(str(v))),
    )


def fetch_historical_bars(
    client: HistoricalDataClient,
    *,
    instrument: Instrument,
    interval: SmartApiInterval,
    from_date: datetime,
    to_date: datetime,
    rate_limiter: RateLimiter,
    max_retries: int = 3,
    retry_delay_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Bar]:
    """Download historical bars via `getCandleData`, rate-limited and
    retried with backoff — exponential on a generic failure, linear when
    the error text indicates a rate limit was hit. Mirrors the pattern in
    `reference/01-v6.py`'s `get_historical_data`.
    """
    params = {
        "exchange": instrument.exchange,
        "symboltoken": instrument.symbol_token,
        "interval": interval.value,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M"),
    }

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            rate_limiter.acquire(sleep=sleep)
            response = client.get_candle_data(params)
        except Exception as exc:  # noqa: BLE001 - broker client errors are untyped
            last_error = exc
            sleep(retry_delay_seconds * (2**attempt))
            continue

        if not response.get("status"):
            message = str(response.get("message", ""))
            last_error = RuntimeError(f"getCandleData failed: {message}")
            if "rate limit" in message.lower():
                sleep(retry_delay_seconds * (attempt + 1))  # linear backoff
            else:
                sleep(retry_delay_seconds * (2**attempt))
            continue

        raw_rows = response.get("data", [])
        assert isinstance(raw_rows, list)
        return [_row_to_bar(instrument.symbol_token, row) for row in raw_rows]

    raise RuntimeError(
        f"failed to fetch historical data for {instrument.trading_symbol} "
        f"after {max_retries} attempts"
    ) from last_error
