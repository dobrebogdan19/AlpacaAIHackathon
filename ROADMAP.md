# ROADMAP.md

Phased build plan. Each task has an acceptance criterion — do not check a box
unless that criterion actually passes.

**Deadline: 4 September 2026.** Phases 0-3 are mandatory. Phase 7 is
non-negotiable: an undeployed project is not a valid submission.

---

## Phase 0 — Walking skeleton

One ugly file that runs end to end. No LLM, no UI, no scheduler.

- [x] **T0.1** `skeleton.py`: hardcoded SMA-crossover strategy, minimal
      backtest, promotion gate, one paper order, printed decision record.
      *Accept:* runs with `DRY_RUN=True` and prints real metrics.
- [x] **T0.2** Run once with `DRY_RUN=False`.
      *Accept:* order appears in the Alpaca paper dashboard.
      Submitted 2026-08-29: 1-share AAPL market BUY, order id
      c62533f9-4cc0-4600-a4bb-62f431ae6c24, status ACCEPTED (market closed,
      queued for next open).

---

## Phase 1 — Reusable foundation

Split the skeleton into modules. Behaviour must not change.

- [x] **T1.1** `schema.py` — Pydantic models for the strategy grammar
      (indicators, operators, conditions, full Strategy object).
      *Accept:* an invalid strategy raises a clear validation error.
      `tests/test_schema.py`: 11 tests (unknown indicator, bad period,
      identical entry/exit, join rules) pass.
- [x] **T1.2** `data.py` — fetch daily bars, cache in SQLite, serve from
      cache on repeat calls.
      *Accept:* second call for the same range makes zero network requests.
      `tests/test_data.py::test_repeat_call_makes_zero_network_requests` and
      `::test_partial_overlap_fetches_only_the_missing_range` pass.
- [x] **T1.3** `engine.py` — the replay engine. Takes a Strategy + bars,
      returns metrics (total return, max drawdown, trade count, win rate,
      equity curve).
      *Accept:* same numbers as `skeleton.py` on the same input.
      `tests/test_engine.py::test_regression_matches_skeleton_numbers_on_aapl`
      passes: 3.84% / 10.78% / 3 trades, and key-for-key parity with
      `skeleton.backtest` on the same bars.
- [x] **T1.4** Tests for the engine — specifically one that would FAIL if
      execution happened on the decision bar instead of the next open.
      *Accept:* 3-4 tests pass; the lookahead test is explicit and named.
      `test_execution_uses_next_bar_open_not_decision_close` (+ final-bar-drop,
      terminal-open-position, round-trip). Lookahead check verified to fail
      (+400% vs 0%) if execution uses the decision bar's close.
- [x] **T1.5** `db.py` — tables for strategies, runs, decisions, shadows.
      *Accept:* schema created on startup, idempotent.
      `tests/test_db.py`: 9 tests. `connect()` runs `CREATE TABLE IF NOT EXISTS`
      x4 (strategies/runs/backtests/decisions) on every open; a second `connect()`
      + explicit `init_db()` neither errors nor wipes rows. Phase-4 values
      (status `retired`, decision outcome `retired`) already fit — no migration.
      Same DB file as `data.py` (`bars_cache.db`).

---

## Phase 2 — Hypothesis generation

- [x] **T2.1** LLM call that emits strategies conforming to `schema.py`.
      *Accept:* 5 valid strategies from one call.
      `generator.py` — OpenAI `gpt-4o-mini` (see D18), grammar embedded as an
      explicit contract built from the schema enums, JSON-only response.
      `scripts/generate_demo.py` produced 5 valid strategies in one call
      (attempts=1, 0 failures, 0 dupes) on 2026-08-29.
- [x] **T2.2** Validation retry loop — invalid output is fed back with the
      error and regenerated, max 2 retries.
      *Accept:* a deliberately broken response recovers automatically.
      `test_generator.py::test_invalid_item_is_fed_back_and_recovered` (broken
      period 999 → retry carries "period ... 250" → 5 valid) and
      `::test_gives_up_after_max_retries_and_logs` (3 attempts, leftover logged).
- [x] **T2.3** Deduplication — near-identical strategies are collapsed.
      *Accept:* generating twice does not fill the DB with duplicates.
      `dedup_key()` = SHA-256 of the canonical form (symbol + entry/exit rules,
      condition order within a rule sorted away; name/rationale excluded).
      `test_near_identical_strategies_collapse` (reordered AND-conditions → 1)
      and `test_generating_twice_does_not_duplicate_in_db` (5, not 10).

---

## Phase 3 — Gate and execution

- [ ] **T3.1** Promotion gate with configurable thresholds; records the
      reason for every promote/reject.
      *Accept:* every candidate has a written reason stored.
- [ ] **T3.2** Order execution through the **Alpaca MCP server**.
      *Accept:* a promoted strategy produces a real paper order via MCP,
      visible in the Alpaca dashboard.
- [ ] **T3.3** Kill switch + position limits checked before every order.
      *Accept:* flipping the switch blocks orders with a logged reason.

> **Checkpoint:** with Phase 3 done, the project is complete and submittable.
> Everything below is upside.

---

## Phase 4 — Regret ledger

- [ ] **T4.1** Rejected candidates persist as shadow portfolios.
      *Accept:* rejected strategies have equity curves alongside active ones.
- [ ] **T4.2** Shadow evaluation — replay each shadow forward on new bars.
      *Accept:* shadows update when new data arrives.
- [ ] **T4.3** Retirement rule — when a shadow beats an active strategy by
      a margin over a window, retire the active one and promote the shadow.
      *Accept:* one demonstrable retirement, triggered on historical data.
- [ ] **T4.4** Post-mortem generation — a written explanation of what was
      missed and why, stored with the retirement.
      *Accept:* readable text, grounded in the actual numbers.
- [ ] **T4.5** Selection-bias check — compare promoted vs rejected average
      forward performance.
      *Accept:* a single number showing whether the gate selects signal
      or noise. Report it honestly even if it is unflattering.

---

## Phase 5 — API

- [ ] **T5.1** FastAPI app: endpoints for strategies, runs, shadows,
      decisions, equity curves.
      *Accept:* all dashboard data reachable over HTTP.
- [ ] **T5.2** `POST /cycle` — trigger a full generate→backtest→gate→execute
      cycle on demand.
      *Accept:* returns a run id and completes without an open market.
- [ ] **T5.3** Scheduler for periodic cycles, with the API still usable
      if the scheduler is not running.
      *Accept:* dashboard renders from stored data alone.

---

## Phase 6 — Dashboard

- [ ] **T6.1** Hero view: active portfolio equity curve plotted among the
      shadow curves.
      *Accept:* understandable from a single screenshot, no explanation.
- [ ] **T6.2** Decision log: promotions, rejections, retirements, each with
      its reason.
- [ ] **T6.3** "Run a new cycle" button wired to `POST /cycle`.
      *Accept:* a visitor can trigger a live cycle and watch it complete.

---

## Phase 7 — Deploy (non-negotiable)

- [ ] **T7.1** Seed the database locally with real generated results.
      *Accept:* fresh deploy shows a full dashboard immediately.
- [ ] **T7.2** Deploy to Railway with persistent volume, env vars set.
      *Accept:* public URL, no login, loads in under 3 seconds.
- [ ] **T7.3** README with setup, architecture, and an honest limitations
      section.

---

## Phase 8 — Submission

- [ ] **T8.1** Demo video (~3 min). Hero shot in the first 20 seconds.
      Show the MCP order path explicitly.
- [ ] **T8.2** Pitch deck (~8 slides): problem, insight, how it works,
      the lookahead/selection-bias rigor, sponsor tech used, limitations,
      what's next.
- [ ] **T8.3** Submit on lablab: live URL, repo, video, deck.

---

## Fallback plan

If behind on Sunday evening, cut in this order:
1. Phase 4 reduced to 2 shadows and one manual retirement example
2. Phase 6 reduced to a single static page
3. Phase 2 reduced to 3 pre-generated strategies

Never cut Phase 7. Ship something smaller that is live and filmed.
