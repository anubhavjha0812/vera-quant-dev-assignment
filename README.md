# Vera Quant

**Quant Developer assignment — Vera Developers Pvt. Ltd.**
Live grid / stop-and-reverse execution engines, technical analysis, a
Macro Regime Engine, Angel One SmartAPI integration, a bar-accurate
backtest harness, idempotent order management, and observability — built
on `Decimal`-only money, no lookahead, and one code path shared by
backtest and live.

**Status:** all 17 steps of the dev plan are built — **219 tests, all
green.** See `DECISIONS.md` for every non-obvious call made along the
way, by whom, and why, and the "Status & before you submit" section below
for what's still pending (your input, not more code).

---

## Table of contents
- [Getting started — replicate & test on your system](#getting-started--replicate--test-on-your-system)
- [Scope & assumptions](#scope--assumptions)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Development workflow](#development-workflow)
- [Design notes](#design-notes)
- [Live trading experience](#live-trading-experience)
- [Status & before you submit](#status--before-you-submit)
- [Appendix: every line of the email → modules and tests](#appendix-every-line-of-the-email--modules-and-tests)

---

## Getting started — replicate & test on your system

### Prerequisites
- Python 3.11+ (3.12 recommended — this was built and tested on 3.12.3)
- [Poetry](https://python-poetry.org/docs/#installation)

### 1. Clone
```bash
git clone https://github.com/anubhavjha0812/vera-quant-dev-assignment.git
cd vera-quant-dev-assignment
```

### 2. Install
```bash
poetry install
```
Installs the package in editable mode plus every dev tool (pytest, ruff,
mypy, pre-commit) into an isolated virtualenv — nothing touches your
system Python.

### 3. Run the test suite
```bash
poetry run pytest -q
```
Expect `219 passed`. This is a fresh-clone, no-network, no-credentials
run — every broker/network interaction in the suite goes through a fake
or mock, never the real internet.

### 4. Run the demo backtest
```bash
poetry run demo-backtest
```
No network, no credentials — synthetic price data through the ATR
stop-and-reverse strategy, the risk layer, and the simulated
(`PaperBroker`) broker, printing fills, final P&L, and the last few trade
blotter entries. This is the literal "one command" bar the dev plan sets
for submission, alongside step 3 above.

### 5. Lint & type-check (optional, same checks CI/hooks run)
```bash
poetry run ruff check .
poetry run mypy src
```
Both should print a clean "all good" with no errors.

### 6. Pre-commit hooks (optional, matches what CI enforces)
```bash
poetry run pre-commit install
```
After this, every `git commit` auto-runs ruff + mypy + whitespace checks
first.

### 7. Docker (optional)
```bash
docker compose up app                 # the same demo, containerised
docker compose up -d redis grafana    # good-to-have infra, not yet wired into app code
```
> ✅ **Build-verified.** `docker compose build` was run on the user's own
> machine (2026-10-02) and completed cleanly — all 7 layers, image
> `veera-app:latest` built successfully. The install-sequence rehearsal
> described in `DECISIONS.md` #23 (done in a sandbox with no Docker
> daemon access) caught and fixed the one real bug beforehand: the
> Dockerfile pinned `poetry==1.8.3`, but `poetry.lock` is lock-version
> 2.1, written by Poetry 2.2.1, which 1.8.x can't read — fixed to
> `poetry==2.2.1`, and the real build confirmed that fix was sufficient.

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
*(Verified to run exactly as written.)* See `tests/test_backtest.py` for
the same wiring exercised by the no-lookahead and golden-file regression
tests.

---

## Scope & assumptions
- **Broker:** Angel One SmartAPI (REST + WebSocket 2.0), approved by HR in
  place of Zerodha Kite Connect named in the original email — confirmed
  in writing.
- **Order placement:** both paper and live are built as independent,
  pluggable broker packages (`brokers/paper/`, `brokers/live_smartapi/`)
  behind one shared `Broker` interface. Neither package imports the
  other; either can be removed and the other keeps working. **Paper is
  the default run mode**; live requires explicit opt-in
  (`BROKER_MODE=live`) plus a separate run-time confirmation.
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
Both `backtest.py` and `live_runner.py` call the exact same `decide`
(strategy), `risk_check` (risk layer) and `Broker` objects — one code
path, not two that could silently drift (CLAUDE.md rule 3).

### Live-ready vs paper-only, and why
| Component | Status | Why |
|---|---|---|
| Strategy engines, TA, risk layer, cost model, backtest harness | **Live-ready** | Pure functions / deterministic objects, fully tested, no network dependency. |
| `brokers/paper/` | **Live-ready** (it's the default) | Fully simulated, no real orders — the safe default (`BROKER_MODE=paper`). |
| `brokers/live_smartapi/` (REST adapter, WebSocket feed) | **Built and tested against a mocked/fake client, not yet run against the real SmartAPI** | Correct SDK method names/response shapes for a few endpoints (`generateTokens`, modify/cancel, the margin calculator) are this adapter's best-effort mapping — only login/placeOrder/position/orderBook/getCandleData are directly evidenced by `reference/01-v6.py`. Needs a short verification pass against the installed `smartapi-python` SDK and real (sandboxed) credentials before `BROKER_MODE=live` is used for real. The `--confirm-live` + `LiveSafetyLimits` gates (CLAUDE.md rule 10) are already in place for when that happens. |
| Cost model (`costs.py`) | **Live-ready, rates sourced and dated (not guessed)** | STT/CTT/brokerage/GST change with the Union Budget — figures were pulled via live web search on 2026-10-01 (training data predates the 1 Feb 2026 Budget's STT hike) and cited per field in `costs.py`. Still pending your own check against Angel One's calculator + contract notes (`DECISIONS.md` #22). |
| Streamlit dashboard, Grafana | **Not built** | Explicitly Good-to-Have in the email; deferred (`DECISIONS.md` #21). FastAPI's status endpoints exist and are tested. |

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
```bash
poetry install
poetry run pre-commit install   # auto-runs the checks below before every commit
poetry run ruff check .         # lint
poetry run mypy src             # types
poetry run pytest -q            # tests
```
`CLAUDE.md` holds the project's hard rules (money never a float, no
lookahead, broker isolation, etc.) and `.claude/agents/` defines the
`test-writer` → implement → `reviewer` loop this codebase was built with —
see that file for the full list.

---

## Design notes

### Vectorisation vs incremental (technical analysis module)
Two different jobs get two different tools, deliberately — using the
wrong one for either is the actual `iterrows()` mistake, not just
literally calling that method:

- **Batch/offline work** — loading a Parquet file, resampling, computing
  a backtest report's summary statistics — uses pandas/numpy
  vectorisation (`market_data.py`'s storage functions, and the reference
  formulas in `tests/test_indicators.py`). The whole series is known up
  front, there's no per-bar state to carry forward, and a vectorised op
  is both simpler to write and faster than a Python loop.
- **The live/backtest hot path** — every indicator in `indicators.py`
  (EMA, ADX, RSI, MACD, ATR, Bollinger, OBV, VWAP) — uses small,
  incremental `.update()`-per-bar objects instead. Vectorisation is the
  *wrong* tool here for two reasons: (1) live trading only ever has "the
  next bar," not a full series to vectorise over, so the two code paths
  (backtest replay, live tick-by-tick) would diverge if backtest used
  pandas and live used something else; one incremental object run
  bar-by-bar in both is what keeps them identical (CLAUDE.md rule 3). (2)
  Recomputing a vectorised indicator over the whole history on every new
  bar, just to read the last value, is O(n) work per bar instead of
  O(1) — the exact cost profile `iterrows()` has a bad reputation for,
  just hidden behind a vectorised call instead of an explicit loop.

### Race-condition stress test (step 13)
`tests/test_websocket_feed.py::test_concurrent_tick_hammering_with_shutdown_is_deterministic`
hammers 200 ticks at `LiveTickSource` from a background thread
(simulating the SmartAPI SDK's own callback thread) while the main thread
drains and shuts down, repeated 5 times. **No race was found** — every
run ends on the same final conflated value. This holds by construction,
not luck: `ConflatingTickBuffer` is a single `dict.__setitem__` per tick
(atomic under the GIL) with no read-modify-write step, so interleaving at
any point still leaves the dict in a valid state, and because the
hammering thread pushes a *fixed, ordered* sequence, "whichever tick
landed last" is always the same tick regardless of scheduling jitter. If
a real implementation used a check-then-act pattern (e.g. "read the dict,
decide, then write") instead, this is exactly where a race would show
up — worth re-running this test after any change to `ConflatingTickBuffer`.

### Tool opinions
The email asks for an opinion on TA-Lib/pandas-ta/vectorbt/backtrader's
limits (Good-to-Have) — here's what actually showed up building this:

- **TA-Lib**: not used. It needs a separate compiled C library installed
  outside pip — exactly the kind of thing that breaks a fresh clone, CI,
  or Docker build in ways that are annoying to debug and have nothing to
  do with the actual trading logic (`DECISIONS.md` #17).
- **pandas-ta**: tried, not used. It requires Python ≥3.12 while this
  project targets `^3.11`; pulling it in as even a test-only dependency
  would have narrowed compatibility for a comparison tool, not core
  logic. `tests/test_indicators.py` validates the incremental indicators
  against a hand-written vectorised reference built directly on pandas'
  `.ewm()`/`.rolling()` instead — same validation, zero extra risk.
- **vectorbt/backtrader**: not used (the dev plan marks this cross-check
  explicitly Optional — `DECISIONS.md` #20). The concrete reason a custom
  harness was worth building instead: `backtest.py`'s fill rule needs a
  specific one-bar execution lag (decide at this bar's close, fill at the
  *next* bar's open — enforced by loop ordering, see that module's
  docstring) plus MCX's quotation-multiplier P&L adjustment
  (`contracts.py`), both project-specific enough that fitting them into a
  general-purpose library's abstractions would likely cost more than
  writing ~150 lines of fill-rule code directly.
- **General pattern across all four**: every one of them is a dependency
  whose version/Python-compatibility risk has to be managed forever after
  for something that's either a dev-time convenience or validated once in
  a test. The project's own indicators/cost-model/broker logic are each
  "a single tested implementation," so there was never a strong pull
  toward an external library duplicating that work.

---

## Live trading experience
*(Good-to-have, email: "live trading experience with your own or someone
else's capital" — e.g. a platform you've run live. Left blank here —
fill this in before submitting; it's your experience to describe, not
something to assert on your behalf.)*

---

## Status & before you submit
All 17 steps are built, 219 tests green. A handful of items need your
input, not more code — each links to where the reasoning already lives:

- [ ] **Cost-model rates**: filled in with sourced, dated figures
      (`DECISIONS.md` #22) — still needs your own check against Angel
      One's live calculator + a few real contract notes before trusting
      "to the paisa" for submission.
- [ ] **"Live trading experience"** section above — left blank for you.
- [ ] **Verify `brokers/live_smartapi/` against the real SmartAPI SDK**
      (exact method names for `generateTokens`, modify/cancel, margin
      calculator) before ever setting `BROKER_MODE=live` for real — see
      "Live-ready vs paper-only" above.
- [x] **Docker build** — verified on your machine 2026-10-02, builds clean.
- [x] **Push to GitHub** and reply to Purnima with the repo link. — done.

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
