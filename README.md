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
_(filled in as each step lands)_

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

## Development
```bash
poetry install
poetry run pre-commit install
poetry run ruff check .
poetry run mypy src
poetry run pytest -q
```

## Development
```bash
poetry install
poetry run pre-commit install
poetry run ruff check .
poetry run mypy src
poetry run pytest -q
```
