# Vera Quant — CLAUDE.md

Quant Developer assignment for Vera Developers Pvt. Ltd. Full 17-step plan and
traceability to the original assignment email:
`reference/Vera_Quant_Dev_Assignment_Plan_SmartAPI.md`.

## Project layout
```
src/vera_quant/   # the installable package (src layout — see README for why)
tests/            # pytest suite, mirrors src/vera_quant/ structure
reference/        # assignment PDFs, the dev plan, a prior SmartAPI script —
                   # NOT linted, NOT part of the package, read-only reference
```

## Commands
```bash
poetry install
poetry run ruff check .      # lint
poetry run mypy src          # types
poetry run pytest -q         # tests
poetry run pre-commit install
```

## Hard rules (non-negotiable)

1. **Money is never a `float`.** Use `Decimal` or integer paise everywhere a
   price, P&L, or cost appears. One rounding policy, defined once. Acceptance
   bar is "matches to the paisa," not "roughly matches."
2. **No lookahead.** Any decision at bar/time `t` may only use data known at
   or before `t`. A run on data cut off at time `t` must make identical
   decisions up to `t` as a run on the full dataset.
3. **Backtest and live share one code path.** The same TA/indicator and
   strategy objects run bar-by-bar in both backtest and live — never two
   separate implementations that could drift apart.
4. **The broker is only used through the `Broker` interface**
   (`vera_quant/brokers/base.py`). Nothing outside the broker factory ever
   imports a concrete broker class directly.
5. **`brokers/paper/` and `brokers/live_smartapi/` never import each other.**
   Each must keep working with the other package deleted or stubbed out. This
   is enforced by a test in each package, not just convention.
6. **SmartAPI rate limits are always respected** — see Appendix A of the dev
   plan. Every call to a rate-limited endpoint goes through the shared
   rate limiter, sized to that endpoint's documented limit.
7. **Every strategy or engine change starts with a failing regression test**
   that would have caught the bug being fixed, written before the fix.
8. **No duplicated math.** One tested implementation per indicator/formula,
   shared by every caller — never re-derive the same calculation in two
   places.
9. **No `iterrows()` in a hot loop.** Vectorise for batch/offline work
   (loading, resampling, reports); use incremental per-bar objects in the
   live/backtest hot path. Vectorisation is the wrong tool there because it
   can't update one bar at a time without recomputing the whole series.
10. **Live order placement is opt-in, never the default.** `BROKER_MODE`
    defaults to `paper`. Switching to `live` requires an explicit config
    value *and* a separate run-time confirmation — never one flag alone.

## Workflow

For every change after this step: **test-writer → implement → reviewer →
green CI** (`poetry run ruff check . && poetry run mypy src && poetry run
pytest -q` all passing). Hooks run lint/type/test automatically after edits
inside `src/` or `tests/`; a failing hook means stop and fix before
continuing, not before committing.
