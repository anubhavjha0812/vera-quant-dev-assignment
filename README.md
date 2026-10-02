# Vera Quant

**Quant Developer assignment — Vera Developers Pvt. Ltd.**
Grid / stop-and-reverse execution engines, technical analysis, a Macro
Regime Engine, Angel One SmartAPI integration, a bar-accurate backtest
harness, idempotent order management, and observability — `Decimal`-only
money, no lookahead, one code path shared by backtest and live.

**Status:** all 17 steps of the dev plan are built — **219 tests, all
green.** See `DECISIONS.md` for every non-obvious call made along the way,
and "Status & before you submit" below for what's still pending.

---

## Table of contents
- [Getting started](#getting-started)
- [Scope & assumptions](#scope--assumptions)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Development workflow](#development-workflow)
- [Design notes](#design-notes)
- [Live trading experience](#live-trading-experience)
- [Status & before you submit](#status--before-you-submit)
- [Appendix: every line of the email → modules and tests](#appendix-every-line-of-the-email--modules-and-tests)

---

## Getting started

### Clone
```bash
git clone https://github.com/anubhavjha0812/vera-quant-dev-assignment.git
cd vera-quant-dev-assignment
```

### Option A — Docker (recommended, build-verified)
```bash
docker compose build          # builds the image
docker compose up app         # runs the demo backtest, containerised
docker compose run --rm app poetry run pytest -q   # 219 tests
```
Confirmed working end-to-end on 2026-10-02 (build + demo + full test suite
all passed in the container). See `DECISIONS.md` #23 if curious about the
one bug this caught (a Poetry version mismatch) before the real build.

### Option B — local, with Poetry
Requires Python 3.11+ and [Poetry](https://python-poetry.org/docs/#installation).
```bash
poetry install                # package + dev tools into an isolated venv
poetry run pytest -q          # 219 tests
poetry run demo-backtest      # synthetic data -> strategy -> risk -> PaperBroker
```
No network or credentials needed for any of the above — every
broker/network interaction in the suite is a fake or mock.

Optional, same checks CI/hooks run:
```bash
poetry run ruff check .          # lint
poetry run mypy src              # types
poetry run pre-commit install    # auto-run both + whitespace checks on every commit
```

### A backtest, wired up programmatically
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

---

## Scope & assumptions
- **Broker:** Angel One SmartAPI (REST + WebSocket 2.0), approved by HR in
  place of Zerodha Kite Connect named in the original email.
- **Order placement:** paper and live are independent, pluggable broker
  packages (`brokers/paper/`, `brokers/live_smartapi/`) behind one shared
  `Broker` interface — neither imports the other, either can be removed
  and the other keeps working. **Paper is the default**; live requires
  explicit opt-in (`BROKER_MODE=live`) plus a separate run-time
  confirmation.
- **Market data:** real SmartAPI historical/live data, plus a synthetic
  generator for deterministic tests.
- **Money:** never a float — Decimal/integer-paise throughout, one
  rounding policy, acceptance criterion is "matches to the paisa."

---

## Architecture
```
                     ┌─────────────┐
   market_data.py ──▶│  BarAggregator / TickSource │
   (synthetic, Parquet,            indicators.py
    getCandleData)                 (EMA, RSI, MACD, ATR,
         │                          Bollinger, ADX, OBV, VWAP)
         ▼                                │
   ┌──────────────────────────────────────▼───────────┐
   │   strategies.py (grid_step / sar_step)            │
   │   regime.py overrides params per macro regime      │
   └──────────────────────┬─────────────────────────────┘
                           │ OrderIntent
                           ▼
                     risk.py (caps, clip,
                     kill switches, circuit breaker)
                           │ OrderIntent | None
                           ▼
            ┌──────────────┴───────────────┐
            │   brokers/base.py (interface)  │
            └──────┬────────────────┬────────┘
                    ▼                ▼
          brokers/paper/     brokers/live_smartapi/
          (fill simulation)  (REST + WebSocket 2.0,
                               never imports paper)
                    │                │
                    └───────┬────────┘
                             ▼
                    order_management.py
                 (idempotency key, SQLite
                  journal, Order state machine,
                  position/P&L from fills only)
                             │
               ┌─────────────┼──────────────┐
               ▼             ▼              ▼
       backtest.py   live_runner.py   observability.py /
       (journal-free,  (journal-backed, alerts.py /
        fast reruns)    reconcile on     reconciliation.py /
                        restart)         status_service.py
```
`backtest.py` and `live_runner.py` call the exact same `decide` (strategy),
`risk_check` (risk layer) and `Broker` objects — one code path, not two
that could silently drift (CLAUDE.md rule 3).

### Live-ready vs paper-only, and why
| Component | Status | Why |
|---|---|---|
| Strategy engines, TA, risk layer, cost model, backtest harness | **Live-ready** | Pure functions / deterministic objects, fully tested, no network dependency. |
| `brokers/paper/` | **Live-ready** (default) | Fully simulated, no real orders. |
| `brokers/live_smartapi/` | **Built + tested against a mocked client; real-API connectivity checked, login not yet successful** | The official SDK + TOTP flow reaches the real SmartAPI endpoint and gets a structured response, but `generateSession` returned "INVALID MPIN" with the credentials tried on 2026-10-02 — not yet logged in for real (see `DECISIONS.md` #24). `placeOrder`/margin-calculator method names are still this adapter's best-effort mapping. `--confirm-live` + `LiveSafetyLimits` gates (CLAUDE.md rule 10) are already in place. |
| Cost model (`costs.py`) | **Live-ready, rates sourced and dated** | Pulled via web search 2026-10-01, cited per field in `costs.py`. Still pending your own check against Angel One's calculator + contract notes (`DECISIONS.md` #22). |
| Streamlit dashboard, Grafana | **Not built** | Good-to-Have, deferred (`DECISIONS.md` #21). FastAPI status endpoints exist and are tested. |

---

## Project layout
```
src/vera_quant/
  money.py, models.py            # Decimal-only money rules, domain dataclasses
  contracts.py                   # scrip master, units, expiry/rollover
  costs.py                       # per-leg transaction cost model
  market_data.py                 # storage, synthetic data, aggregation, historical DL
  rate_limiter.py                # shared sliding-window limiter
  indicators.py                  # 8 incremental technical indicators
  strategies.py                  # ATR grid + stop-and-reverse
  risk.py                        # caps, 5 kill switches, flatten
  order_management.py            # idempotency, journal, state machine, P&L
  regime.py                      # Macro Regime Engine
  brokers/
    base.py                      # the Broker interface — no implementation
    paper/                       # simulated fills — never imports live_smartapi
    live_smartapi/                # REST + WebSocket 2.0 — never imports paper
  backtest.py                    # journal-free harness + walk-forward
  live_runner.py                 # journal-backed session, broker factory, reconciliation
  observability.py, alerts.py,   # blotter, phone alerts, reconciliation report,
  reconciliation.py,             # FastAPI status service
  status_service.py
  cli.py                         # `poetry run demo-backtest`
tests/                           # pytest suite, mirrors src/vera_quant/
reference/                       # assignment PDFs, the dev plan, a prior SmartAPI
                                  # script — not linted, not part of the package
```

---

## Development workflow
`CLAUDE.md` holds the project's hard rules (money never a float, no
lookahead, broker isolation, etc.); `.claude/agents/` defines the
`test-writer` → implement → `reviewer` loop this codebase was built with.

---

## Design notes

### Vectorisation vs incremental (technical analysis module)
- **Batch/offline work** (loading Parquet, resampling, report stats) uses
  pandas/numpy vectorisation — the whole series is known up front, no
  per-bar state to carry.
- **The live/backtest hot path** — every indicator in `indicators.py`
  (EMA, ADX, RSI, MACD, ATR, Bollinger, OBV, VWAP) — uses small,
  incremental `.update()`-per-bar objects instead. Vectorisation is wrong
  there: live trading only ever has "the next bar," so a vectorised
  backtest path and a tick-by-tick live path would diverge; one
  incremental object run bar-by-bar in both keeps them identical
  (CLAUDE.md rule 3). Recomputing a vectorised indicator over the whole
  history just to read the last value is also O(n) per bar instead of
  O(1) — the same cost `iterrows()` is criticised for, just hidden behind
  a vectorised call.

### Race-condition stress test (step 13)
`test_concurrent_tick_hammering_with_shutdown_is_deterministic` hammers
200 ticks at `LiveTickSource` from a background thread while the main
thread drains and shuts down, repeated 5 times. **No race found** — every
run ends on the same final value, by construction: `ConflatingTickBuffer`
is a single `dict.__setitem__` per tick (atomic under the GIL), no
read-modify-write step. A check-then-act pattern instead is exactly where
a race would show up — worth re-running this test after any change there.

### Tool opinions
- **TA-Lib**: not used — needs a separate compiled C library outside pip,
  breaks fresh-clone/CI/Docker builds (`DECISIONS.md` #17).
- **pandas-ta**: tried, not used — requires Python ≥3.12 while this
  project targets `^3.11`. `test_indicators.py` validates against a
  hand-written reference on pandas' own `.ewm()`/`.rolling()` instead.
- **vectorbt/backtrader**: not used (explicitly Optional in the dev plan,
  `DECISIONS.md` #20). `backtest.py`'s fill rule needs a specific one-bar
  execution lag plus MCX's quotation-multiplier P&L adjustment — both
  project-specific enough that a custom ~150-line fill engine was cheaper
  than fitting them into a general-purpose library's abstractions.
- **Overall**: each is a dependency with ongoing version/compatibility
  risk for something that's a dev-time convenience at best — the
  project's own indicators/cost-model/broker logic are each "a single
  tested implementation" already.

---

## Live trading experience
*(Good-to-have, email: "live trading experience with your own or someone
else's capital." Left blank — your experience to describe, not something
to assert on your behalf.)*

---

## Status & before you submit
All 17 steps are built, 219 tests green.

- [ ] **Cost-model rates**: sourced, dated figures in place
      (`DECISIONS.md` #22) — still needs your own check against Angel
      One's calculator + contract notes before trusting "to the paisa."
- [ ] **"Live trading experience"** section above — left blank for you.
- [ ] **SmartAPI live verification**: real-API connectivity checked, but
      login itself failed ("INVALID MPIN") with the credentials tried —
      double-check `ANGEL_PASSWORD` in `.env` is your current MPIN, then
      retry (`DECISIONS.md` #24). `placeOrder`/margin-calculator method
      names are still best-effort — verify before `BROKER_MODE=live`.
- [x] **Docker build** — verified on your machine 2026-10-02.
- [x] **Push to GitHub** and reply to Purnima — done.

---

## Appendix: every line of the email → modules and tests
Mirrors Appendix B of `reference/Vera_Quant_Dev_Assignment_Plan_SmartAPI.md`,
updated to point at the actual files.

| Email requirement | Modules | Key tests |
|---|---|---|
| Live grid and stop-and-reverse engines: ATR spacing, pyramiding | `strategies.py` | `test_strategies.py` |
| Kill switches, position caps | `risk.py` | `test_risk.py` |
| Technical analysis module, single tested implementation each | `indicators.py` | `test_indicators.py` |
| Macro Regime Engine: proxies, regime states, overrides, circuit breakers | `regime.py`, `risk.py` | `test_regime.py` |
| Broker and data integration (SmartAPI, REST + WebSocket) | `brokers/live_smartapi/` | `test_smartapi_broker.py`, `test_websocket_feed.py` |
| Tick vendors, data integration | `market_data.py` | `test_market_data.py` |
| MCX and NSE contract masters, expiry and rollover | `contracts.py` | `test_contracts.py` |
| Backtest harness: fills, slippage, costs, walk-forward, no lookahead | `backtest.py`, `brokers/paper/`, `costs.py` | `test_backtest.py`, `test_paper_broker.py`, `test_costs.py` |
| Backtest ↔ live ↔ desk spreadsheet reconciliation | `reconciliation.py`, `live_runner.py` | `test_reconciliation.py`, `test_live_runner.py` |
| Idempotent placement, order-state reconciliation after restarts | `order_management.py`, `live_runner.py` | `test_order_management.py`, `test_live_runner.py` |
| Position and P&L truth, crash recovery | `order_management.py` | `test_order_management.py` |
| Structured logs, trade blotters, phone alerts | `logging.py`, `observability.py`, `alerts.py` | `test_observability.py`, `test_alerts.py` |
| Write tests: every strategy change ships with a regression test | `.claude/agents/test-writer.md`, `reviewer.md` | every `test_*.py` |
| Make agents to automate the SDLC; develop with Claude Code | `CLAUDE.md`, `.claude/` | — |
| Must-have: Python (pandas, numpy, dataclasses, type hints, poetry, pytest) | throughout | `pyproject.toml` |
| Must-have: concurrency (WebSocket, queues, back-pressure, shutdown) | `brokers/live_smartapi/websocket_feed.py` | `test_websocket_feed.py` |
| Must-have: API integration (auth, rate limits, retries, idempotency) | `brokers/live_smartapi/`, `rate_limiter.py` | `test_smartapi_broker.py`, `test_rate_limiter.py` |
| Must-have: Indian market plumbing (MCX, NSE, margins, STT/CTT) | `contracts.py`, `costs.py` | `test_contracts.py`, `test_costs.py` |
| Must-have: numerical discipline (matches to the paisa) | `money.py` | `test_money.py` |
| Must-have: Git (meaningful commits, code review) | — | `git log`, `DECISIONS.md` |
| Must-have: knowing when vectorisation is the wrong answer | `indicators.py` | "Design notes" above |
| Must-have: debugged a race condition (stress test + README note) | `brokers/live_smartapi/websocket_feed.py` | `test_websocket_feed.py`, "Design notes" above |
| Good-to-have: live trading experience | — | "Live trading experience" above *(you to fill in)* |
| Good-to-have: TA-Lib/pandas-ta/vectorbt/backtrader opinions | — | "Design notes" above |
| Good-to-have: Postgres/TimescaleDB/DuckDB/Parquet, Redis, Docker | `market_data.py` (Parquet), `Dockerfile`, `docker-compose.yml` | `test_market_data.py` |
| Good-to-have: Streamlit/FastAPI dashboards; Grafana | `status_service.py` (FastAPI built; Streamlit/Grafana deferred, `DECISIONS.md` #21) | `test_status_service.py` |
| Good-to-have: tick-level data pipelines | `market_data.py` (`ParquetReplayTickSource`) | `test_market_data.py` |
| Deadline: submit by 1 October 2026 | — | — |
