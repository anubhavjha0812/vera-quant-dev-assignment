"""Incremental (update-per-bar) technical indicators.

One tested implementation per indicator, built on three shared primitives
(`_Smoother`/`Ema`/`WilderSmoother`, `TrueRangeCalculator`, `RollingWindow`)
so no formula is written twice (CLAUDE.md rule 8). Every indicator exposes
`.update(...)`, called once per bar; the same object runs bar-by-bar in
both backtest and live, so the two code paths cannot drift apart (CLAUDE.md
rule 3). No `iterrows()` — see the "Vectorisation vs incremental" note in
README.md for why batch pandas is used elsewhere but not here.

No external indicator library (TA-Lib or pandas-ta) is a dependency — see
DECISIONS.md #17. Correctness is checked in tests/test_indicators.py
against a vectorised reference built directly from pandas' own
`.ewm()`/`.rolling()`.
"""
from __future__ import annotations

from collections import deque
from datetime import date
from decimal import Decimal

from vera_quant.models import Bar

# ------------------------------------------------------------------ primitives


class _Smoother:
    """Generic exponential smoother: `value = alpha*x + (1-alpha)*value`,
    seeded with the first input. EMA uses alpha = 2/(n+1); Wilder's
    smoothing (RSI, ATR, ADX) uses alpha = 1/n — one formula, two alphas,
    so it is written once.
    """

    def __init__(self, alpha: Decimal) -> None:
        if not (Decimal(0) < alpha <= Decimal(1)):
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self._alpha = alpha
        self.value: Decimal | None = None

    def update(self, x: Decimal) -> Decimal:
        if self.value is None:
            self.value = x
        else:
            self.value = self._alpha * x + (Decimal(1) - self._alpha) * self.value
        return self.value


class Ema(_Smoother):
    """Exponential moving average — also the trend indicator on its own."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError(f"period must be >= 1, got {period}")
        super().__init__(Decimal(2) / Decimal(period + 1))


class WilderSmoother(_Smoother):
    """Wilder's smoothing (alpha = 1/period) — the recursive average used
    inside RSI, ATR and ADX."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError(f"period must be >= 1, got {period}")
        super().__init__(Decimal(1) / Decimal(period))


class TrueRangeCalculator:
    """Incremental True Range. Needs one bar of state (the previous close)."""

    def __init__(self) -> None:
        self._prev_close: Decimal | None = None

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        return tr


class RollingWindow:
    """Fixed-size window of the last `size` values (simple, not
    exponential, moving statistics) — used where an indicator needs a
    plain rolling mean/std rather than smoothing (Bollinger Bands).
    """

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError(f"size must be >= 1, got {size}")
        self._size = size
        self._values: deque[Decimal] = deque(maxlen=size)

    def update(self, value: Decimal) -> None:
        self._values.append(value)

    @property
    def ready(self) -> bool:
        return len(self._values) == self._size

    @property
    def mean(self) -> Decimal:
        return sum(self._values, Decimal(0)) / len(self._values)

    @property
    def std(self) -> Decimal:
        """Population standard deviation (divides by N, matching the usual
        Bollinger Band convention), not the sample std (N-1)."""
        m = self.mean
        variance = sum(((v - m) ** 2 for v in self._values), Decimal(0)) / len(self._values)
        return variance.sqrt()


# ------------------------------------------------------------------- trend


class Adx:
    """Average Directional Index (Wilder's smoothing), incremental."""

    def __init__(self, period: int = 14) -> None:
        self._prev_high: Decimal | None = None
        self._prev_low: Decimal | None = None
        self._tr = TrueRangeCalculator()
        self._smoothed_tr = WilderSmoother(period)
        self._smoothed_plus_dm = WilderSmoother(period)
        self._smoothed_minus_dm = WilderSmoother(period)
        self._adx_smoother = WilderSmoother(period)
        self.value: Decimal | None = None

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        tr = self._tr.update(high, low, close)

        if self._prev_high is None or self._prev_low is None:
            self._prev_high, self._prev_low = high, low
            self._smoothed_tr.update(tr)
            self._smoothed_plus_dm.update(Decimal(0))
            self._smoothed_minus_dm.update(Decimal(0))
            return None

        up_move = high - self._prev_high
        down_move = self._prev_low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else Decimal(0)
        minus_dm = down_move if (down_move > up_move and down_move > 0) else Decimal(0)
        self._prev_high, self._prev_low = high, low

        smoothed_tr = self._smoothed_tr.update(tr)
        smoothed_plus_dm = self._smoothed_plus_dm.update(plus_dm)
        smoothed_minus_dm = self._smoothed_minus_dm.update(minus_dm)

        if smoothed_tr == 0:
            return self.value

        plus_di = Decimal(100) * smoothed_plus_dm / smoothed_tr
        minus_di = Decimal(100) * smoothed_minus_dm / smoothed_tr
        di_sum = plus_di + minus_di
        dx = Decimal(100) * abs(plus_di - minus_di) / di_sum if di_sum != 0 else Decimal(0)

        self.value = self._adx_smoother.update(dx)
        return self.value


# ---------------------------------------------------------------- momentum


class Rsi:
    """Relative Strength Index (Wilder's smoothing of gains/losses)."""

    def __init__(self, period: int = 14) -> None:
        self._prev_close: Decimal | None = None
        self._avg_gain = WilderSmoother(period)
        self._avg_loss = WilderSmoother(period)
        self.value: Decimal | None = None

    def update(self, bar: Bar) -> Decimal | None:
        close = bar.close
        if self._prev_close is None:
            self._prev_close = close
            return None

        change = close - self._prev_close
        self._prev_close = close
        gain = max(change, Decimal(0))
        loss = max(-change, Decimal(0))
        avg_gain = self._avg_gain.update(gain)
        avg_loss = self._avg_loss.update(loss)

        if avg_loss == 0:
            self.value = Decimal(100)
        else:
            rs = avg_gain / avg_loss
            self.value = Decimal(100) - Decimal(100) / (Decimal(1) + rs)
        return self.value


class Macd:
    """MACD: fast EMA minus slow EMA, plus a signal-line EMA of that."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        self._fast = Ema(fast)
        self._slow = Ema(slow)
        self._signal = Ema(signal)
        self.macd_line: Decimal | None = None
        self.signal_line: Decimal | None = None
        self.histogram: Decimal | None = None

    def update(self, bar: Bar) -> tuple[Decimal, Decimal, Decimal]:
        fast_val = self._fast.update(bar.close)
        slow_val = self._slow.update(bar.close)
        macd_line = fast_val - slow_val
        signal_line = self._signal.update(macd_line)
        histogram = macd_line - signal_line
        self.macd_line, self.signal_line, self.histogram = macd_line, signal_line, histogram
        return macd_line, signal_line, histogram


# --------------------------------------------------------------- volatility


class Atr:
    """Average True Range: Wilder's smoothing of True Range."""

    def __init__(self, period: int = 14) -> None:
        self._tr = TrueRangeCalculator()
        self._smoother = WilderSmoother(period)
        self.value: Decimal | None = None

    def update(self, bar: Bar) -> Decimal:
        tr = self._tr.update(bar.high, bar.low, bar.close)
        self.value = self._smoother.update(tr)
        return self.value


class BollingerBands:
    """Rolling mean +/- `num_std` population standard deviations."""

    def __init__(self, period: int = 20, num_std: Decimal = Decimal(2)) -> None:
        self._window = RollingWindow(period)
        self._num_std = num_std
        self.middle: Decimal | None = None
        self.upper: Decimal | None = None
        self.lower: Decimal | None = None

    def update(self, bar: Bar) -> tuple[Decimal, Decimal, Decimal] | None:
        self._window.update(bar.close)
        if not self._window.ready:
            return None
        self.middle = self._window.mean
        band = self._num_std * self._window.std
        self.upper = self.middle + band
        self.lower = self.middle - band
        return self.middle, self.upper, self.lower


# ------------------------------------------------------------------- volume


class Obv:
    """On Balance Volume: signed cumulative volume by close direction."""

    def __init__(self) -> None:
        self._prev_close: Decimal | None = None
        self.value: Decimal = Decimal(0)

    def update(self, bar: Bar) -> Decimal:
        if self._prev_close is not None:
            if bar.close > self._prev_close:
                self.value += bar.volume
            elif bar.close < self._prev_close:
                self.value -= bar.volume
        self._prev_close = bar.close
        return self.value


class Vwap:
    """Session Volume Weighted Average Price — resets automatically when
    a bar's date differs from the previous bar's (a new trading session).
    """

    def __init__(self) -> None:
        self._session_date: date | None = None
        self._cum_tp_vol = Decimal(0)
        self._cum_vol = Decimal(0)
        self.value: Decimal | None = None

    def update(self, bar: Bar) -> Decimal:
        bar_date = bar.timestamp.date()
        if self._session_date != bar_date:
            self._session_date = bar_date
            self._cum_tp_vol = Decimal(0)
            self._cum_vol = Decimal(0)

        typical_price = (bar.high + bar.low + bar.close) / Decimal(3)
        self._cum_tp_vol += typical_price * bar.volume
        self._cum_vol += bar.volume
        self.value = self._cum_tp_vol / self._cum_vol if self._cum_vol != 0 else typical_price
        return self.value
