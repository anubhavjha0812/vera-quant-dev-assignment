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

## Development
```bash
poetry install
poetry run pre-commit install
poetry run ruff check .
poetry run mypy src
poetry run pytest -q
```
