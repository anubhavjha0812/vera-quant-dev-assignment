"""Each incremental indicator is checked against an independent vectorised
reference built directly from pandas' own `.ewm()`/`.rolling()` (no TA-Lib,
no pandas-ta — see DECISIONS.md #17). Comparisons use a small tolerance
since the reference runs in float and the production code runs in Decimal.
"""
from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest

from vera_quant.indicators import (
    Adx,
    Atr,
    BollingerBands,
    Ema,
    Macd,
    Obv,
    RollingWindow,
    Rsi,
    TrueRangeCalculator,
    Vwap,
    WilderSmoother,
)
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Bar

PERIODS = 80
TOL = 0.05  # generous: Decimal vs float drift compounds over 80 recursive steps


def _bars() -> list[Bar]:
    return generate_synthetic_bars(
        instrument_token="1",
        start=datetime(2026, 1, 2, 9, 15),
        periods=PERIODS,
        pattern="chop",
        seed=11,
    )


def _series(bars: list[Bar]):
    return (
        pd.Series([float(b.close) for b in bars]),
        pd.Series([float(b.high) for b in bars]),
        pd.Series([float(b.low) for b in bars]),
        pd.Series([float(b.volume) for b in bars]),
    )


def _close(a: Decimal | None, b: float, tol: float = TOL) -> bool:
    if a is None:
        return False
    return abs(float(a) - b) <= tol


# ------------------------------------------------------------- primitives --


def test_ema_primitive_matches_pandas_ewm_adjust_false() -> None:
    bars = _bars()
    close, *_ = _series(bars)
    ref = close.ewm(span=10, adjust=False).mean()

    ema = Ema(period=10)
    for i, bar in enumerate(bars):
        value = ema.update(bar.close)
        assert _close(value, ref.iloc[i])


def test_wilder_smoother_matches_pandas_ewm_alpha() -> None:
    bars = _bars()
    close, *_ = _series(bars)
    period = 14
    ref = close.ewm(alpha=1 / period, adjust=False).mean()

    smoother = WilderSmoother(period=period)
    for i, bar in enumerate(bars):
        value = smoother.update(bar.close)
        assert _close(value, ref.iloc[i])


def test_true_range_first_bar_is_high_minus_low() -> None:
    calc = TrueRangeCalculator()
    tr = calc.update(high=Decimal("105"), low=Decimal("100"), close=Decimal("102"))
    assert tr == Decimal("5")


def test_true_range_uses_previous_close_once_available() -> None:
    calc = TrueRangeCalculator()
    calc.update(high=Decimal("105"), low=Decimal("100"), close=Decimal("104"))
    # Gap up: high-low=3, but high-prev_close=6 dominates.
    tr = calc.update(high=Decimal("110"), low=Decimal("107"), close=Decimal("108"))
    assert tr == Decimal("6")


def test_rolling_window_mean_and_population_std() -> None:
    window = RollingWindow(size=3)
    for v in ("10", "20", "30"):
        window.update(Decimal(v))
    assert window.ready
    assert window.mean == Decimal(20)
    # population variance = ((10-20)^2+(0)+(10)^2)/3 = 200/3 -> std = sqrt(200/3)
    assert abs(window.std - Decimal("8.164965809")) < Decimal("0.0001")


def test_rolling_window_not_ready_until_full() -> None:
    window = RollingWindow(size=3)
    window.update(Decimal("1"))
    assert not window.ready


# -------------------------------------------------------------------- RSI --


def test_rsi_matches_wilder_reference_after_warmup() -> None:
    bars = _bars()
    close, *_ = _series(bars)
    period = 14
    delta = close.diff()
    # Leave index 0 as NaN (no prior close) so the ewm seeds on the first
    # *real* gain/loss at index 1 — exactly what the incremental Rsi does
    # by only starting its WilderSmoother from bar 1 onward.
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    ref = 100 - 100 / (1 + rs)

    rsi = Rsi(period=period)
    values: list[Decimal | None] = [rsi.update(bar) for bar in bars]

    for i in range(period * 2, PERIODS):
        assert _close(values[i], ref.iloc[i])


def test_rsi_returns_none_on_the_very_first_bar() -> None:
    rsi = Rsi()
    bars = _bars()
    assert rsi.update(bars[0]) is None


# ------------------------------------------------------------------- MACD --


def test_macd_matches_ema_difference_reference() -> None:
    bars = _bars()
    close, *_ = _series(bars)
    fast_ref = close.ewm(span=12, adjust=False).mean()
    slow_ref = close.ewm(span=26, adjust=False).mean()
    macd_ref = fast_ref - slow_ref
    signal_ref = macd_ref.ewm(span=9, adjust=False).mean()

    macd = Macd()
    for i, bar in enumerate(bars):
        macd_line, signal_line, histogram = macd.update(bar)
        assert _close(macd_line, macd_ref.iloc[i])
        assert _close(signal_line, signal_ref.iloc[i])
        assert _close(histogram, macd_ref.iloc[i] - signal_ref.iloc[i])


# -------------------------------------------------------------------- ATR --


def test_atr_matches_wilder_true_range_reference_after_warmup() -> None:
    bars = _bars()
    close, high, low, _ = _series(bars)
    period = 14
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    tr.iloc[0] = float(high.iloc[0] - low.iloc[0])
    ref = tr.ewm(alpha=1 / period, adjust=False).mean()

    atr = Atr(period=period)
    values = [atr.update(bar) for bar in bars]
    for i in range(period * 2, PERIODS):
        assert _close(values[i], ref.iloc[i])


# -------------------------------------------------------------------- ADX --


def test_adx_matches_wilder_dmi_reference_after_warmup() -> None:
    bars = _bars()
    _, high, low, _ = _series(bars)
    period = 14

    up_move = high.diff().fillna(0.0)
    down_move = (-low.diff()).fillna(0.0)
    plus_dm = pd.Series(
        [u if (u > d and u > 0) else 0.0 for u, d in zip(up_move, down_move, strict=True)]
    )
    minus_dm = pd.Series(
        [d if (d > u and d > 0) else 0.0 for u, d in zip(up_move, down_move, strict=True)]
    )

    close_for_tr = pd.Series([float(b.close) for b in bars])
    prev_close = close_for_tr.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    tr.iloc[0] = float(high.iloc[0] - low.iloc[0])

    smoothed_tr = tr.ewm(alpha=1 / period, adjust=False).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=1 / period, adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * smoothed_plus_dm / smoothed_tr
    minus_di = 100 * smoothed_minus_dm / smoothed_tr
    di_sum = plus_di + minus_di
    dx = (100 * (plus_di - minus_di).abs() / di_sum).where(di_sum != 0, 0.0)
    ref = dx.ewm(alpha=1 / period, adjust=False).mean()

    adx = Adx(period=period)
    values = [adx.update(bar.high, bar.low, bar.close) for bar in bars]
    for i in range(period * 3, PERIODS):
        assert _close(values[i], ref.iloc[i], tol=0.5)  # ADX is 0-100 scale


# ----------------------------------------------------------------- Bollinger


def test_bollinger_matches_rolling_mean_and_population_std() -> None:
    bars = _bars()
    close, *_ = _series(bars)
    period = 20
    ref_mid = close.rolling(period).mean()
    ref_std = close.rolling(period).std(ddof=0)

    bb = BollingerBands(period=period, num_std=Decimal(2))
    for i, bar in enumerate(bars):
        result = bb.update(bar)
        if i < period - 1:
            assert result is None
        else:
            assert result is not None
            middle, upper, lower = result
            assert _close(middle, ref_mid.iloc[i])
            assert _close(upper, ref_mid.iloc[i] + 2 * ref_std.iloc[i])
            assert _close(lower, ref_mid.iloc[i] - 2 * ref_std.iloc[i])


# ----------------------------------------------------------------------- OBV


def test_obv_matches_signed_cumulative_volume_reference() -> None:
    bars = _bars()
    close, _, _, volume = _series(bars)
    direction = close.diff().apply(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0))
    ref = (direction.fillna(0.0) * volume).cumsum()

    obv = Obv()
    for i, bar in enumerate(bars):
        value = obv.update(bar)
        assert _close(value, ref.iloc[i], tol=1.0)  # volume units, not price


# ---------------------------------------------------------------------- VWAP


def test_vwap_matches_cumulative_typical_price_reference_single_session() -> None:
    bars = _bars()  # all within one trading session (same calendar day)
    close, high, low, volume = _series(bars)
    typical = (high + low + close) / 3
    ref = (typical * volume).cumsum() / volume.cumsum()

    vwap = Vwap()
    for i, bar in enumerate(bars):
        value = vwap.update(bar)
        assert _close(value, ref.iloc[i])


def test_vwap_resets_on_a_new_session() -> None:
    bars_day1 = generate_synthetic_bars(
        instrument_token="1", start=datetime(2026, 1, 2, 9, 15), periods=5, seed=1
    )
    bars_day2 = generate_synthetic_bars(
        instrument_token="1", start=datetime(2026, 1, 3, 9, 15), periods=1, seed=1
    )
    vwap = Vwap()
    for bar in bars_day1:
        vwap.update(bar)
    day2_value = vwap.update(bars_day2[0])
    expected = (bars_day2[0].high + bars_day2[0].low + bars_day2[0].close) / 3
    assert abs(day2_value - expected) < Decimal("0.01")


def test_indicators_reject_non_positive_periods() -> None:
    for ctor in (lambda: Ema(0), lambda: WilderSmoother(0), lambda: RollingWindow(0)):
        with pytest.raises(ValueError):
            ctor()
