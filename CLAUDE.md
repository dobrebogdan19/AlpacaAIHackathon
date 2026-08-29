# CLAUDE.md

Project context for Claude Code. Read this before any task in this repo.

---

## What this project is

An autonomous trading agent on Alpaca that **audits its own decisions**.

The loop:
1. Generate candidate strategies (LLM, constrained grammar)
2. Backtest each on historical daily bars
3. Promote only those passing a fixed gate
4. Execute promoted strategies on the Alpaca **paper** account
5. Track rejected candidates as **shadow portfolios**
6. When a shadow consistently beats an active strategy, retire the active one
   and write a post-mortem

Built for the Alpaca AI Trading Agents Hackathon (lablab.ai), deadline
**4 September 2026**. Optimize for a working, demoable system — not for
production hardening.

**What we claim:** the agent revises its decisions when evidence contradicts them.
**What we do NOT claim:** that it learns, predicts better over time, or generates alpha.
Never write copy, comments, or docs that overstate this.

---

## Hard constraints — never violate

### Correctness
- **No lookahead bias, ever.** Decisions are made on bar N's CLOSE.
  Execution happens at bar N+1's OPEN. Any code that decides and executes
  on the same bar is a bug, not a shortcut.
- **No backtesting libraries.** No backtrader, vectorbt, zipline, bt.
  The replay engine is a plain loop we own and can explain. Simple and
  correct beats sophisticated and subtly wrong.
- Long only. One position per strategy at a time. No shorting, no leverage,
  no options, no margin.
- Fixed notional per position. Fixed commission assumption (may be zero,
  but state it explicitly in the code).

### Safety
- Credentials come from `.env` via python-dotenv. Never hardcode keys,
  never log them, never send them to the frontend.
- `.env` is gitignored. If you ever see it staged, stop and flag it.
- **Paper trading only.** `paper=True` everywhere. Never construct a client
  pointed at the live endpoint, even in examples or comments.
- Every module that can submit orders respects a `DRY_RUN` flag.
- A global kill switch must be checkable before any order submission.

### Scope discipline
- This ships in days, not weeks. Prefer the simplest thing that is correct
  and demonstrable.
- Do not add: authentication, multi-user support, CI pipelines, Docker
  orchestration, ORM abstractions, or caching layers beyond a plain
  SQLite table.
- Do not refactor working code for elegance. Ask first.

---

## Strategy grammar (closed vocabulary)

Strategies are **data, not code**. The LLM never writes Python. It emits a
structure validated by Pydantic; anything that fails validation is rejected
and regenerated.

Allowed indicators: `SMA`, `EMA`, `RSI`, `ATR`, `MOMENTUM`, `VOLUME_AVG`
Allowed operators: `>`, `<`, `CROSSES_ABOVE`, `CROSSES_BELOW`
Allowed composition: a single condition, or two joined by `AND` / `OR`.

Adding a new indicator means: extend the enum, implement it in the engine,
add a test. Never let a strategy reference something the engine can't compute.

---

## Stack

- Python 3.13, FastAPI, SQLite, `alpaca-py`
- Alpaca **MCP server** for the agent's order execution path (this is the
  sponsor integration and must be visible in the demo — do not replace it
  with plain SDK calls)
- Alpaca historical bars for all backtesting and shadow evaluation
- Deployment: Railway
- Docs reference: `https://docs.alpaca.markets/llms-full.txt`

---

## Data notes

- Free tier gives **IEX** data, not SIP. Fine for daily bars. Do not build
  anything that depends on intraday tick fidelity.
- Cache fetched bars in SQLite. Never re-fetch the same range twice in a run.
- The market is closed on weekends. Nothing in the system may require an
  open market to produce output — the demo must work at any hour.

---

## Working agreement

- Before implementing anything non-trivial, state your plan in 3-5 lines
  and wait for confirmation.
- After each task, append an entry to `DECISIONS.md` for any choice that
  a reviewer might question. Keep entries short: decision, reason,
  alternative rejected.
- Update `ROADMAP.md` checkboxes as tasks complete. Do not mark a task done
  unless its acceptance criterion actually passes.
- When something is ambiguous, ask rather than assume. A wrong assumption
  here costs more than a question.
