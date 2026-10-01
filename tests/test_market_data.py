import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from vera_quant.contracts import ScripMaster
from vera_quant.market_data import (
    BarAggregator,
    ParquetReplayTickSource,
    SmartApiInterval,
    drop_incomplete_last_bar,
    fetch_historical_bars,
    generate_synthetic_bars,
    read_bars_parquet,
    read_ticks_parquet,
    stitch_continuous_series,
    write_bars_parquet,
    write_ticks_parquet,
)
from vera_quant.models import Bar, Tick

FIXTURE = Path(__file__).parent / "fixtures" / "scrip_master_sample.json"


def _bar(token: str, ts: datetime, price: str, volume: int = 100) -> Bar:
    p = Decimal(price)
    return Bar(
        instrument_token=token, timestamp=ts, open=p, high=p, low=p, close=p, volume=volume
    )


def _tick(token: str, ts: datetime, price: str, qty: int = 1) -> Tick:
    return Tick(instrument_token=token, timestamp=ts, last_price=Decimal(price), last_quantity=qty)


# ---------------------------------------------------------------- storage --


def test_bars_round_trip_through_parquet_preserve_decimal(tmp_path: Path) -> None:
    bars = [
        _bar("1", datetime(2026, 1, 2, 9, 15), "100.03"),
        _bar("1", datetime(2026, 1, 2, 9, 20), "100.57"),
    ]
    path = tmp_path / "bars.parquet"
    write_bars_parquet(bars, path)
    loaded = read_bars_parquet(path)
    assert loaded == bars
    assert all(isinstance(b.open, Decimal) for b in loaded)


def test_ticks_round_trip_through_parquet(tmp_path: Path) -> None:
    ticks = [
        _tick("1", datetime(2026, 1, 2, 9, 15, 1), "100.05", qty=5),
        _tick("1", datetime(2026, 1, 2, 9, 15, 2), "100.10", qty=3),
    ]
    path = tmp_path / "ticks.parquet"
    write_ticks_parquet(ticks, path)
    loaded = read_ticks_parquet(path)
    assert loaded == ticks


# ------------------------------------------------------------- synthetic --


def test_synthetic_bars_are_deterministic_for_the_same_seed() -> None:
    kwargs = dict(instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=20, seed=42)
    a = generate_synthetic_bars(**kwargs)  # type: ignore[arg-type]
    b = generate_synthetic_bars(**kwargs)  # type: ignore[arg-type]
    assert a == b


def test_synthetic_bars_trend_up_is_strictly_increasing() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=30,
        pattern="trend_up",
        seed=1,
    )
    closes = [b.close for b in bars]
    assert all(closes[i] > closes[i - 1] for i in range(1, len(closes)))


def test_synthetic_bars_trend_down_is_strictly_decreasing() -> None:
    bars = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=30,
        pattern="trend_down",
        seed=1,
    )
    closes = [b.close for b in bars]
    assert all(closes[i] < closes[i - 1] for i in range(1, len(closes)))


def test_synthetic_bars_vol_spike_widens_average_range_in_the_spike_window() -> None:
    periods = 40
    bars = generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=periods,
        pattern="vol_spike",
        seed=7,
    )
    spike_width = max(3, periods // 5)
    spike_start, spike_end = periods // 3, periods // 3 + spike_width
    spike_ranges = [bars[i].high - bars[i].low for i in range(spike_start, spike_end)]
    calm_ranges = [bars[i].high - bars[i].low for i in range(0, spike_start)]
    avg_spike = sum(spike_ranges, Decimal(0)) / len(spike_ranges)
    avg_calm = sum(calm_ranges, Decimal(0)) / len(calm_ranges)
    # Spike amplitude (2.0) is 20x calm amplitude (0.10); averaged over a
    # multi-bar window this comfortably beats any single-bar random draw.
    assert avg_spike > avg_calm * 2


def test_synthetic_bars_rejects_unknown_pattern() -> None:
    with pytest.raises(ValueError):
        generate_synthetic_bars(
            instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=5, pattern="nonsense"
        )


def test_real_and_synthetic_bars_feed_the_same_consumer(tmp_path: Path) -> None:
    """step 6's Done-when: real (recorded) and synthetic data feed one bar
    iterator — both are plain list[Bar], consumed identically."""
    synthetic = generate_synthetic_bars(
        instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=5, seed=3
    )
    path = tmp_path / "recorded.parquet"
    write_bars_parquet(synthetic, path)
    recorded = read_bars_parquet(path)  # stands in for "real" downloaded data

    def total_volume(bars: list[Bar]) -> int:
        return sum(b.volume for b in bars)

    assert total_volume(synthetic) == total_volume(recorded)
    assert all(isinstance(b, Bar) for b in synthetic + recorded)


# --------------------------------------------------------- bar aggregator --


def test_aggregator_builds_ohlcv_from_ticks_in_one_bucket() -> None:
    agg = BarAggregator(instrument_token="1", interval_minutes=5)
    base = datetime(2026, 1, 2, 9, 15, 0)
    assert agg.on_tick(_tick("1", base, "100", qty=10)) is None
    assert agg.on_tick(_tick("1", base.replace(second=30), "101", qty=5)) is None
    assert agg.on_tick(_tick("1", base.replace(minute=17), "99", qty=7)) is None

    bar = agg.flush()
    assert bar is not None
    assert bar.open == Decimal("100")
    assert bar.high == Decimal("101")
    assert bar.low == Decimal("99")
    assert bar.close == Decimal("99")
    assert bar.volume == 22


def test_aggregator_returns_completed_bar_when_a_new_bucket_starts() -> None:
    agg = BarAggregator(instrument_token="1", interval_minutes=5)
    base = datetime(2026, 1, 2, 9, 15, 0)
    agg.on_tick(_tick("1", base, "100", qty=1))
    completed = agg.on_tick(_tick("1", base.replace(minute=20), "102", qty=1))
    assert completed is not None
    assert completed.close == Decimal("100")
    assert completed.timestamp == datetime(2026, 1, 2, 9, 15, 0)


def test_aggregator_flush_with_no_ticks_returns_none() -> None:
    agg = BarAggregator(instrument_token="1", interval_minutes=5)
    assert agg.flush() is None


# -------------------------------------------------------------- stitching --


def test_stitch_continuous_series_switches_contract_at_roll_date() -> None:
    master = ScripMaster.from_file(FIXTURE)
    chain = master.futures_chain("MCX", "GOLD")
    near, far = chain[0], chain[1]

    bars_by_token = {
        near.symbol_token: [
            _bar(near.symbol_token, datetime(2025, 11, 1), "100"),
            _bar(near.symbol_token, datetime(2025, 12, 2), "105"),  # inside near's roll window
        ],
        far.symbol_token: [
            _bar(far.symbol_token, datetime(2025, 11, 1), "200"),
            _bar(far.symbol_token, datetime(2025, 12, 2), "205"),
        ],
    }
    stitched = stitch_continuous_series(bars_by_token, chain, roll_buffer_days=5)
    assert [b.close for b in stitched] == [Decimal("100"), Decimal("205")]


def test_stitch_continuous_series_rejects_empty_chain() -> None:
    with pytest.raises(ValueError):
        stitch_continuous_series({}, [], roll_buffer_days=5)


# ------------------------------------------------------- historical fetch --


class _FakeHistoricalClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, str]] = []

    def get_candle_data(self, params: dict[str, str]) -> dict:
        self.calls.append(params)
        return next(self._responses)


def _nifty_instrument():
    master = ScripMaster.from_file(FIXTURE)
    inst = master.get_by_symbol("NFO", "NIFTY25DECFUT")
    assert inst is not None
    return inst


def test_fetch_historical_bars_happy_path() -> None:
    from vera_quant.rate_limiter import RateLimiter

    client = _FakeHistoricalClient(
        [
            {
                "status": True,
                "data": [
                    ["2026-01-02T09:15:00", "100.00", "101.00", "99.50", "100.50", "1000"],
                    ["2026-01-02T09:20:00", "100.50", "102.00", "100.00", "101.75", "1200"],
                ],
            }
        ]
    )
    bars = fetch_historical_bars(
        client,
        instrument=_nifty_instrument(),
        interval=SmartApiInterval.FIVE_MINUTE,
        from_date=datetime(2026, 1, 2, 9, 0),
        to_date=datetime(2026, 1, 2, 10, 0),
        rate_limiter=RateLimiter(max_requests=3, window_seconds=1.0),
        sleep=lambda _s: None,
    )
    assert len(bars) == 2
    assert bars[0].open == Decimal("100.00")
    assert client.calls[0]["interval"] == "FIVE_MINUTE"
    assert client.calls[0]["symboltoken"] == _nifty_instrument().symbol_token


def test_fetch_historical_bars_retries_then_succeeds() -> None:
    from vera_quant.rate_limiter import RateLimiter

    client = _FakeHistoricalClient(
        [
            {"status": False, "message": "some transient error"},
            {
                "status": True,
                "data": [["2026-01-02T09:15:00", "100", "101", "99", "100.5", "1000"]],
            },
        ]
    )
    bars = fetch_historical_bars(
        client,
        instrument=_nifty_instrument(),
        interval=SmartApiInterval.FIVE_MINUTE,
        from_date=datetime(2026, 1, 2, 9, 0),
        to_date=datetime(2026, 1, 2, 10, 0),
        rate_limiter=RateLimiter(max_requests=10, window_seconds=1.0),
        retry_delay_seconds=0.001,
        sleep=lambda _s: None,
    )
    assert len(bars) == 1
    assert len(client.calls) == 2


def test_fetch_historical_bars_raises_after_max_retries() -> None:
    from vera_quant.rate_limiter import RateLimiter

    client = _FakeHistoricalClient([{"status": False, "message": "down"}] * 3)
    with pytest.raises(RuntimeError):
        fetch_historical_bars(
            client,
            instrument=_nifty_instrument(),
            interval=SmartApiInterval.FIVE_MINUTE,
            from_date=datetime(2026, 1, 2, 9, 0),
            to_date=datetime(2026, 1, 2, 10, 0),
            rate_limiter=RateLimiter(max_requests=10, window_seconds=1.0),
            max_retries=3,
            retry_delay_seconds=0.001,
            sleep=lambda _s: None,
        )


def test_drop_incomplete_last_bar_removes_the_in_progress_candle() -> None:
    bars = [
        _bar("1", datetime(2026, 1, 2, 9, 15), "100"),
        _bar("1", datetime(2026, 1, 2, 9, 20), "101"),
    ]
    result = drop_incomplete_last_bar(bars, as_of=datetime(2026, 1, 2, 9, 20, 30))
    assert result == bars[:-1]


def test_drop_incomplete_last_bar_keeps_all_when_last_bar_already_closed() -> None:
    bars = [
        _bar("1", datetime(2026, 1, 2, 9, 15), "100"),
        _bar("1", datetime(2026, 1, 2, 9, 20), "101"),
    ]
    result = drop_incomplete_last_bar(bars, as_of=datetime(2026, 1, 2, 9, 26, 0))
    assert result == bars


# -------------------------------------------------------------- TickSource --


def test_parquet_replay_tick_source_yields_recorded_ticks_in_order(tmp_path: Path) -> None:
    ticks = [
        _tick("1", datetime(2026, 1, 2, 9, 15, 1), "100"),
        _tick("1", datetime(2026, 1, 2, 9, 15, 2), "101"),
    ]
    path = tmp_path / "recorded_ticks.parquet"
    write_ticks_parquet(ticks, path)
    source = ParquetReplayTickSource(path)

    async def collect() -> list[Tick]:
        return [t async for t in source.ticks()]

    replayed = asyncio.run(collect())
    assert replayed == ticks
