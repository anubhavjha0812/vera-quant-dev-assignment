# Decisions log

Every non-obvious call made while building this assignment, who made it, and
why — so the README/submission can point here instead of re-explaining
context, and so a later step doesn't silently contradict an earlier one.
Newest entries at the bottom. "By" is **You** (Anubhav), **Claude**
(recommended and you didn't push back), or **Joint** (discussed, you decided).

| # | Decision | By | Why / context |
|---|---|---|---|
| 1 | Broker: Angel One SmartAPI (REST + WebSocket 2.0) in place of Zerodha Kite Connect named in the original email. | You | HR-approved; confirmed to Purnima in writing (Step 1). |
| 2 | Both **paper** and **live** order placement get built as real, working modes — not paper-only. | You | You have a registered static IP, so the plan's original "paper-only, no static IP" blocker no longer applies. |
| 3 | Paper and live are two fully **independent, pluggable broker packages** (`brokers/paper/`, `brokers/live_smartapi/`) behind one shared `Broker` interface. Neither imports the other; deleting either one leaves the other working. | You | Explicit requirement — not just a config `if/else` branch, a real plug-in boundary. Enforced in CLAUDE.md rule 5 and tested in steps 10-12. |
| 4 | `BROKER_MODE` defaults to `paper`; `live` requires the config flag **and** a separate run-time confirmation, plus hard size/count caps. | Claude | Real-money safety net now that live placement is in scope — a bug shouldn't be able to flip live trading on by itself. |
| 5 | `src/` layout (`src/vera_quant/`), not a flat layout. | Claude | Was already in the original dev plan; also means tests exercise the *installed* package, catching packaging bugs (relevant for step 17's Docker/fresh-clone requirement) instead of accidentally importing off `sys.path`. |
| 6 | Structured JSON logging via stdlib `logging` + a custom `JsonFormatter` — not `logzero` (used in `reference/01-v6.py`). | Claude (you said "jo tumhe sahi lage") | Mail explicitly asks for **structured** logs for alerting/reconciliation (step 16); `logzero`'s default output is human-readable colored text, not machine-parseable JSON, and getting it there would mean rebuilding the same formatter anyway plus an extra dependency. |
| 7 | Docker: build it, but only at **Step 17**, kept thin (app + optional Redis/Grafana). | Claude | Listed as "Good to Have" in the email, not a Must-Have — core engine steps take priority given the deadline. |
| 8 | Proceed with the **full 17-step plan** despite the submission deadline being today (1 Oct 2026, the same day work started). | You ("lets try our best") | Flagged the risk; you chose not to pre-cut scope. The plan's own Step-11 checkpoint + Step-17 fallback ("list unbuilt steps as designed but not built") stays as the safety net if time runs out. |
| 9 | A brand-new **standalone git repo** inside `veera/`, not committing into the parent `Alaka-Research-Quant-Developer-Intern-Assignment` repo it was initially nested in. | You | That parent repo is an unrelated prior assignment for a different company; mixing histories would look wrong in a submission to Purnima. |
| 10 | New repo name: `vera-quant-dev-assignment`. | You | — |
| 11 | New repo visibility: **private** for now; add Purnima as a collaborator or make it public closer to/at submission. | You | — |
| 12 | No `Co-Authored-By: Claude...` attribution footer on commits in this repo. | You | Submission repo to a potential employer; you didn't want AI co-authorship in the commit history. (Saved as a standing preference for this project.) |
| 13 | Cost model (`costs.py`, step 5) ships with **all rates as placeholder `Decimal("0")`**, not Claude's best-guess STT/CTT/brokerage/GST figures. | You | Those rates change almost every Union Budget and the step's own acceptance bar is "matches to the paisa" against your real Angel One contract notes — a wrong guess would silently fail exactly the precision check the assignment is testing. Tests so far verify calculation *structure* only, with clearly-labeled synthetic rates; real numbers + a reference-sheet test are still **pending from you**. |
| 14 | `reference/01-v6.py` (your earlier working Angel One bot) is used as a **pattern reference** for broker/API conventions — login flow, scrip-master URL, `getCandleData` param shape, `placeOrder` shape, retry/backoff style — but NOT copied for logging style, threading model, float-money handling, or linear instrument lookups. | Joint | You said to keep referencing it; Claude flagged which parts meet this project's stricter bar (CLAUDE.md rules) and which don't, so it's reused selectively, not wholesale. |
| 15 | MCX quotation-multiplier table (`contracts.py`) only has a verified entry for **Gold (100)** — unlisted commodities default to multiplier 1 rather than a guessed value. | Claude | Same precision-risk reasoning as #13: fabricating a multiplier for a commodity Claude isn't certain of would silently corrupt P&L for that instrument. Add more entries once verified against the exchange's contract spec. |
| 16 | This file (`DECISIONS.md`) exists and gets updated as the project continues. | You | Keep a single place tracking both your calls and Claude's, instead of them being scattered across chat history. |
| 17 | No external indicator library (**neither TA-Lib nor pandas-ta**) is a dependency anywhere. Step 7's tests compare the incremental per-bar implementation against a plain pandas/numpy reference formula written directly in the test file. | You + Claude | You ruled out TA-Lib (needs a separate compiled C library — breaks fresh-clone/CI/Docker setups). `pandas-ta` was the fallback, but it requires Python ≥3.12 while this project targets `^3.11`, and bumping the floor just for a test-only comparison wasn't worth narrowing compatibility. A hand-written vectorised reference gives the same validation with zero extra dependency and zero version risk. |

| 18 | ATR grid algorithm (step 8): a **breakout/pyramiding** grid — adds a unit every `k_spacing*atr` the price moves favourably from a cycle anchor (up to `max_units`), and mirrors that level count back down on retrace (gives back a unit per level). Not a countertrend/averaging-down grid. | Claude | The email/plan name "ATR-based spacing, pyramiding" but don't specify the exact grid algorithm — there's no single standard one. Pyramiding specifically means adding to a *winning* position, which this does; documented precisely in `strategies.py`'s docstring so it's an explicit, testable spec rather than a guess. Flag if you intended the countertrend (buy-the-dip) variant instead — it'd be a different function. |

## Still pending a decision from you

- Real cost-model rates (STT, CTT, Angel One brokerage, exchange transaction
  charges, SEBI fee, stamp duty, GST) — see #13.
- More MCX quotation multipliers beyond Gold, if other commodities get
  traded — see #15.
- When to flip the new GitHub repo from private to shared/public, and who
  to add as a collaborator (#11).
