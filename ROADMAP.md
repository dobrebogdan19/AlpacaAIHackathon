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

- [x] **T3.1** Promotion gate with configurable thresholds; records the
      reason for every promote/reject.
      *Accept:* every candidate has a written reason stored.
      `gate.py` — 3 thresholds in one dict; `min_trades` raised 3→10 (D21).
      `gate.evaluate()` returns a reason naming every failed threshold, for
      promote and reject alike; `cycle.run_cycle` writes a `decisions` row for
      every candidate (`test_cycle.py::test_every_candidate_gets_a_decision_row`:
      4 candidates → 4 decision rows, all reasons non-empty). `test_gate.py`:
      7 tests.
- [x] **T3.2** Order execution through the **Alpaca MCP server**.
      *Accept:* a promoted strategy produces a real paper order via MCP,
      visible in the Alpaca dashboard.
      `mcp_client.py` drives `alpaca-mcp-server` (v2.3.0) over a stdio subprocess
      via `fastmcp.Client` and calls `place_stock_order` (D23). `scripts/
      run_cycle_demo.py` on 2026-08-29 promoted 2 strategies and submitted 2
      real paper orders through MCP — ids `54c5bdf6-3c57-4c93-b01d-67cfae7b86c3`
      (SPY) and `7320049b-1c53-4d24-98ad-8b6faff75c67` (AAPL), both `accepted`,
      persisted to `orders` with `submitted_via='mcp'`. Logs show every MCP hop.
      `test_mcp_client.py`: 9 tests (offline).
- [x] **T3.3** Kill switch + position limits checked before every order.
      *Accept:* flipping the switch blocks orders with a logged reason.
      `risk.check()` runs before every order in `cycle.py` (D24): env-or-DB kill
      switch, `MAX_CONCURRENT_POSITIONS=3`, `MAX_NOTIONAL_PER_POSITION=2000`,
      `DRY_RUN` honoured. Verified live: `KILL_SWITCH=1 python scripts/
      run_cycle_demo.py` → both promoted strategies logged
      "ORDER BLOCKED — kill switch engaged", `orders.status='blocked'`, no order
      sent. `test_risk.py`: 12 tests.

Runner: `cycle.run_cycle(...)` — one function, generate→backtest→gate→execute
→persist, no `__main__`-only logic, `generate_fn` injectable (D26). Phase 5
(`POST /cycle`) calls it directly.

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

Built before Phase 4 — see D27 (a live URL is the non-negotiable deliverable).

- [x] **T5.1** FastAPI app: endpoints for strategies, runs, decisions,
      equity curves. *(shadows are Phase 4.)*
      *Accept:* all dashboard data reachable over HTTP.
      `api.py` — `GET /api/{health,runs,runs/{id},strategies,strategies/{id},
      orders,equity-curves}`, every one a pure `SELECT` (D6, D28).
      `tests/test_api.py`: 9 offline tests (TestClient, temp DB).
- [x] **T5.2** `POST /cycle` — trigger a full generate→backtest→gate→execute
      cycle on demand.
      *Accept:* returns a run id and completes without an open market.
      `POST /api/cycle` → 202 + `{run_id}`, runs in a background task,
      single-flighted + rate-limited to 1 / 60s (D28). Poll `GET /api/runs/{id}`
      (`finished_at` is the done signal). Verified against the seed sequence
      (`scripts/seed.py`) — market closed, orders queue.
- [ ] **T5.3** Scheduler for periodic cycles, with the API still usable
      if the scheduler is not running.
      *Accept:* dashboard renders from stored data alone.
      *Deferred:* the dashboard already renders from stored data with no
      scheduler running (D6), and `POST /api/cycle` covers on-demand runs for
      the demo. A cron/APScheduler loop is post-submission upside.

---

## Phase 6 — Dashboard

One static file, no build step — `static/index.html`, vanilla `fetch` + inline
SVG (D29). Served at `/` by `api.py`.

- [x] **T6.1** Hero view: equity curves of the promoted strategies plotted
      together. *(shadow curves are Phase 4.)*
      *Accept:* understandable from a single screenshot, no explanation.
      Inline SVG from `GET /api/equity-curves`, normalised to % return, legend
      per strategy, zero line marked.
- [x] **T6.2** Decision log: every candidate with PROMOTED/REJECTED and its
      exact reason, grouped by run. *(retirements are Phase 4.)*
      The centrepiece of the page.
- [x] **T6.3** "Run a new cycle" button wired to `POST /api/cycle`.
      *Accept:* a visitor can trigger a live cycle and watch it complete.
      Button → 202, polls `GET /api/runs/{id}` every 2s, refreshes all panels
      when `finished_at` is set.

---

## Phase 7 — Deploy (non-negotiable)

- [x] **T7.1** Seed the database locally with real generated results.
      *Accept:* fresh deploy shows a full dashboard immediately.
      `scripts/seed.py` builds `seed.db` (committed; `!seed.db` gitignore
      exception, D30) — 4 runs: promoted 5/1/3, rejected 3/3/3/5, with 3 real
      MCP paper orders (broker ids) + `blocked` rows from the position cap.
      `api._bootstrap_db()` copies it onto the volume on first boot.
- [x] **T7.2** Deploy with a persistent DB path and env vars set.
      *Accept:* public URL, no login. (Warm loads are sub-second; Render free
      spins down after ~15 min idle and cold-starts in ~30–60 s — accepted.)
      **Live: https://alpaca-self-audit.onrender.com/**
      Host: **Render** (Railway free plan blocked — resource limit, and the one
      project is an unrelated app; see D32). `render.yaml` Blueprint, free tier,
      `DB_PATH=/tmp/trading.db` re-seeded from committed `seed.db` on each cold
      start (D30). Verified 2026-08-29: `/api/health` 200 (bootstrap fired,
      4 seed runs present); **`/api/mcp-check` 200 — the Alpaca MCP stdio
      subprocess runs in the Render Linux container** (real paper account
      returned, ~19 s cold); dashboard + all read endpoints serve; a full cycle
      (run 5) completed on the instance in 32 s. No SDK fallback was needed.
- [x] **T7.3** README with setup, architecture, and an honest limitations
      section. `README.md` — architecture diagram, env-var table, local run,
      deploy steps, and a limitations section (paper only, IEX, small sample,
      selection bias not yet measured, no learning claim per D9).

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
