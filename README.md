# Vera Quant — Quant Developer Assignment

## Scope & assumptions
- **Broker:** Angel One SmartAPI (REST + WebSocket 2.0), approved by HR in place of
  Zerodha Kite Connect named in the original email — confirmed in writing.
- **Order placement:** both paper and live are built as independent, pluggable
  broker packages (`brokers/paper/`, `brokers/live_smartapi/`) behind one shared
  `Broker` interface. Neither package imports the other; either can be removed
  and the other keeps working. **Paper is the default run mode**; live requires
  explicit opt-in (`BROKER_MODE=live`) plus a separate run-time confirmation.
- **Market data:** real SmartAPI historical/live data, plus a synthetic generator
  for deterministic tests.
- **Money:** never a float — Decimal/integer-paise throughout, one rounding
  policy, acceptance criterion is "matches to the paisa."

## Status
Work in progress — see `reference/Vera_Quant_Dev_Assignment_Plan_SmartAPI.md`
for the full 17-step development plan and traceability back to the original
assignment email.

## Project layout
```
src/vera_quant/   # the installable package (src layout)
tests/            # pytest test suite
reference/        # assignment PDFs, the dev plan, and an existing SmartAPI
                   # reference script — not linted, not part of the package
```

## How to run

### A backtest (paper broker, synthetic data)
```python
from datetime import datetime
from decimal import Decimal

from vera_quant.backtest import run_backtest
from vera_quant.brokers.paper import PaperBroker
from vera_quant.costs import CostRateSchedule
from vera_quant.indicators import Atr
from vera_quant.market_data import generate_synthetic_bars
from vera_quant.models import Instrument
from vera_quant.risk import RiskParams, RiskState, risk_gate
from vera_quant.strategies import SarParams, SarState, sar_step

instrument = Instrument(
    exchange="NFO", trading_symbol="NIFTY25DECFUT", symbol_token="26000",
    tick_size=Decimal("0.05"), lot_size=25,
)
bars = generate_synthetic_bars(
    instrument_token="26000", start=datetime(2026, 1, 2, 9, 15),
    periods=100, pattern="trend_up", seed=1,
)

atr, sar_state, sar_params = Atr(period=5), SarState(), SarParams()
def decide(bar, position):
    return sar_step(bar, atr.update(bar) or Decimal(0), position, sar_state, sar_params)

risk_state, risk_params = RiskState(), RiskParams(
    max_position_per_instrument=100, max_total_position=100, max_order_size=100,
    max_daily_loss=Decimal("-1e6"), max_drawdown_pct=Decimal("0.99"),
    reject_storm_max_rejects=999, reject_storm_window_seconds=60,
    stale_data_max_seconds=999999,
)
def risk_check(intent, position, now):
    risk_state.last_data_timestamp = now
    return risk_gate(intent, position, abs(position.net_quantity), risk_state, risk_params, now=now)

broker = PaperBroker(slippage_ticks=Decimal(1), cost_schedule=CostRateSchedule(), is_option=False)
result = run_backtest(bars, instrument, decide, risk_check, broker)
print(f"{len(result.fills)} fills, final P&L {result.final_position.realized_pnl}")
```
See `tests/test_backtest.py` for the same wiring exercised by the
no-lookahead and golden-file regression tests.

## Vectorisation vs incremental (technical analysis module)
Two different jobs get two different tools, deliberately — using the wrong
one for either is the actual `iterrows()` mistake, not just literally
calling that method:

- **Batch/offline work** — loading a Parquet file, resampling, computing a
  backtest report's summary statistics — uses pandas/numpy vectorisation
  (`market_data.py`'s storage functions, and the reference formulas in
  `tests/test_indicators.py`). The whole series is known up front, there's
  no per-bar state to carry forward, and a vectorised op is both simpler to
  write and faster than a Python loop.
- **The live/backtest hot path** — every indicator in `indicators.py`
  (EMA, ADX, RSI, MACD, ATR, Bollinger, OBV, VWAP) — uses small, incremental
  `.update()`-per-bar objects instead. Vectorisation is the *wrong* tool
  here for two reasons: (1) live trading only ever has "the next bar," not
  a full series to vectorise over, so the two code paths (backtest replay,
  live tick-by-tick) would diverge if backtest used pandas and live used
  something else; one incremental object run bar-by-bar in both is what
  keeps them identical (CLAUDE.md rule 3). (2) Recomputing a vectorised
  indicator over the whole history on every new bar, just to read the last
  value, is O(n) work per bar instead of O(1) — the exact cost profile
  `iterrows()` has a bad reputation for, just hidden behind a vectorised
  call instead of an explicit loop.

## Race-condition stress test (step 13)
`tests/test_websocket_feed.py::test_concurrent_tick_hammering_with_shutdown_is_deterministic`
hammers 200 ticks at `LiveTickSource` from a background thread (simulating
the SmartAPI SDK's own callback thread) while the main thread drains and
shuts down, repeated 5 times. **No race was found** — every run ends on
the same final conflated value. This holds by construction, not luck:
`ConflatingTickBuffer` is a single `dict.__setitem__` per tick (atomic
under the GIL) with no read-modify-write step, so interleaving at any
point still leaves the dict in a valid state, and because the hammering
thread pushes a *fixed, ordered* sequence, "whichever tick landed last"
is always the same tick regardless of scheduling jitter. If a real
implementation used a check-then-act pattern (e.g. "read the dict, decide,
then write") instead, this is exactly where a race would show up — worth
re-running this test after any change to `ConflatingTickBuffer`.

## Development
```bash
poetry install
poetry run pre-commit install
poetry run ruff check .
poetry run mypy src
poetry run pytest -q
```
