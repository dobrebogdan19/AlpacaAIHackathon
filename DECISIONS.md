# DECISIONS.md

Architectural decision log. One entry per choice a reviewer might question.
Keep entries short: what was decided, why, what was rejected.

Append new entries at the bottom. Never rewrite history — if a decision is
reversed, add a new entry that supersedes the old one and say so.

---

### D1 — Execution on the next bar's open, not the decision bar's close
Decisions are computed from bar N's close; fills happen at bar N+1's open.
Deciding and filling on the same bar is the most common way backtests
produce fictional returns. This costs realism nothing and buys correctness.
*Rejected:* same-bar close execution (simpler, but silently wrong).

### D2 — No backtesting library
The replay engine is a hand-written loop. A library we don't fully understand
would give results we can't defend under questioning, and its assumptions
about fills and ordering are exactly where the errors hide.
*Rejected:* backtrader, vectorbt.

### D3 — Strategies are validated data, not generated code
The LLM emits a structure conforming to a closed grammar (fixed indicators
and operators), validated by Pydantic. It never writes executable Python.
This converts the riskiest component — free-form model output — into the
most controlled one, and removes an arbitrary code execution surface.
*Rejected:* letting the model emit Python expressions to be eval'd.

### D4 — Long only, one position at a time, fixed notional
Shorting, leverage, and position sizing each add failure modes that consume
build time without changing what the demo shows.
*Rejected:* full portfolio construction.

### D5 — Daily bars, not intraday
The free tier serves IEX data, which is a small fraction of total volume.
Intraday bars from IEX are thin enough to be misleading. Daily bars are
sound, and the strategy horizon does not need finer resolution.

### D6 — The dashboard reads only from stored data
All results are computed ahead of time and persisted. Rendering never
depends on a live market, a running scheduler, or a background process.
A judge opening the link at 2am on a Sunday sees a full dashboard.
*Rejected:* computing on page load.

### D7 — Orders route through the Alpaca MCP server
The agent's execution path uses the MCP server rather than direct SDK calls.
This is the sponsor's agent-facing surface and is the integration the
hackathon is about; it is also more honest about what "agent" means here.
The SDK is still used for bulk historical data, where MCP adds nothing.

### D8 — Paper trading only, with DRY_RUN and a kill switch
No live endpoint is ever constructed anywhere in the codebase.

### D9 — The claim is revision, not learning
The system does not learn in any ML sense — no weights, no training. It
performs continuous evidence-based selection: shadows are evaluated forward,
and active strategies are retired when a shadow demonstrably beats them.
Copy, comments, and the pitch must say "revises decisions when evidence
contradicts them", never "learns from mistakes".
*Reason:* the stronger claim is not supported by 7 days of data and would
not survive an informed question.

### D10 — Report the selection-bias metric even if unflattering
Comparing forward performance of promoted vs rejected strategies can show
that the promotion gate selects noise rather than signal. That result is
reported as-is. Honest negative results are more credible than a curated
success story, and the failure mode itself is the interesting finding.

### D11 — A terminal open position is unrealized, not a trade
If a strategy is still holding on the final bar, the backtest no longer
fabricates an exit at the last close and counts it in `trades`. That was an
accounting artifact — the strategy never generated that exit — and it
inflated the trade count, which the promotion gate reads directly.
Now: `num_trades` and `win_rate_pct` cover realized (exit-signal) trades
only; a still-open position is marked to market at the final close and
reported as `open_position` + `unrealized_pnl_pct`. `total_return_pct` still
includes that mark-to-market value (final equity = mtm value when open), so
returns stay comparable, but the gate can no longer be passed by a position
that was never actually closed.
*Rejected:* forcing a synthetic exit at the last close (simpler, but lets an
un-exited position satisfy a trade-count threshold).
*Note:* on the current AAPL SMA(10/30) run the strategy happens to end flat,
so this changes no numbers today — it removes a latent way to game the gate.

### D12 — `skeleton.py` is frozen as the regression oracle
Phase 1 split the skeleton into `schema.py` / `data.py` / `engine.py` without
touching `skeleton.py`. It stays in the repo, untested-against-network, purely
as the reference implementation. `test_engine.py::test_regression...` runs both
`skeleton.backtest` and `engine.run_backtest` on the same committed bar fixture
and asserts key-for-key equality. Porting surfaced **no bugs** in the skeleton —
today's numbers (3.84% / 10.78% / 3 trades) were correct.
*Rejected:* deleting the skeleton once modules existed (loses the oracle).

### D13 — Regression fixture is a committed JSON snapshot, not a live fetch
`tests/fixtures/aapl_daily_2026-08-29.json` holds the 250 AAPL daily bars the
skeleton fetched on 2026-08-29. The regression test reads this file, so it is
deterministic, offline, and stable as "now" moves past the hackathon.
*Rejected:* fetching AAPL in the test (non-deterministic, needs network and
keys in CI, and the reference numbers drift every day).

### D14 — `data.py` caches queried *ranges*, not just bars
A `bar_coverage` table records which `(symbol, timeframe, [start,end])` windows
have been requested. Without it, weekends/holidays (no row) would look like
cache misses forever and re-trigger fetches. A repeat call whose range is
fully covered makes zero network calls; a partial-overlap call fetches only
the uncovered sub-range(s). The single network entry point is
`data._fetch_from_alpaca`, which tests monkeypatch.
*Rejected:* inferring coverage from min/max cached bar dates (breaks on gaps
at the edges of a request).

### D15 — Engine indicators computed generically; only SMA has an oracle
`engine.run_backtest` dispatches on the `IndicatorName` enum. SMA/EMA/RSI/ATR/
MOMENTUM/VOLUME_AVG are all implemented (EMA seeded with the first-N SMA, RSI
and ATR with Wilder smoothing, MOMENTUM as percent change, VOLUME_AVG as an
SMA of volume). Only SMA is validated against `skeleton.py`; the others match
the grammar but have no reference implementation yet. A missing OHLC/volume
field raises a clear `ValueError` rather than guessing.
*Rejected:* implementing only SMA now (engine must not be hardcoded per D3 /
CLAUDE.md) — and hiding the others behind `NotImplementedError` (they're small
and standard).

### D16 — Insufficient indicator history evaluates a rule to False
Matching `skeleton.py`'s `None not in (...)` guard: if any indicator a
condition needs has too few bars (including the bar-before-first for a
crossover), that condition is False and no signal fires. This keeps the warm-up
period behaviour identical to the skeleton.

### D17 — `test_conn.py` / `test_data.py` moved to `scripts/`, `pytest.ini` deleted
The two root-level scripts were manual connectivity probes (they hit the network
on import), not tests — and `tests/test_data.py` already exists, so the name
collision was a trap. They are now `scripts/check_connection.py` /
`scripts/check_data.py`. With no `test_*.py` left at the root, the `testpaths =
tests` guard in `pytest.ini` had nothing left to exclude, so the file is gone.
*Rejected:* keeping the scripts as `test_*` with a pytest-ignore rule (fragile).

### D18 — One SQLite file; plain `sqlite3`; Phase 4 attaches without a migration
`db.py` uses the same file as `data.py`'s bar cache (`bars_cache.db`) and the
stdlib `sqlite3` only (no ORM, per CLAUDE.md). Tables: `strategies`, `runs`,
`backtests`, `decisions`. Shadow tracking (Phase 4) needs no new DDL: a rejected
candidate is a `strategies` row (`status='rejected'`) with its curve in
`backtests`; a forward shadow replay is another `backtests` row (later `run_id`,
later `bars_end`); a retirement is `status='retired'` + a `decisions` row with
`outcome='retired'`. The `status` and `outcome` CHECK constraints already list
those values.
*Rejected:* a separate `shadows` table now (premature — Phase 4 may want a
different shape), and inferring "covered" state without explicit columns.

### D19 — Strategy generation uses OpenAI `gpt-4o-mini`, not the Anthropic API
T2.1 as written said "via the Anthropic API"; the build uses OpenAI
`gpt-4o-mini` (`generator.MODEL`, one constant) reading `OPENAI_API_KEY` from
`.env`. Reason: that is the key the operator supplied. The choice is provider-
agnostic in spirit — the model only ever emits grammar-constrained JSON (D3),
never code, so the provider is not load-bearing. `MODEL` is a single string to
change if we swap back.
*Rejected:* Anthropic `claude-*` (original task text) — no key available.
*Note:* CLAUDE.md's "Stack" section never named an LLM provider, so nothing
there is contradicted.

### D20 — Retries recover invalid output only; dedup by canonical hash
`generator.generate()` retries (max 2) **only** to fix items that failed Pydantic
validation — each is fed back with its specific error. If the model simply
returns fewer *unique* strategies than asked, we keep what we got rather than
churn the API for the count. Deduplication is a SHA-256 over a canonical form
(symbol + entry/exit rules, with the two conditions of an AND/OR rule sorted so
operand order can't fake a new strategy; name and rationale excluded), stored as
`strategies.dedup_key` under a partial `UNIQUE` index (NULL for manual entries).
`insert_strategy` returns the existing id on a key hit, so re-running generation
never duplicates rows.
*Rejected:* retrying to top up the count (scope creep, burns tokens); comparing
full JSON (defeated by reordering and by name/rationale differences).

### D21 — `min_trades` gate raised from 3 to 10; all-fail is reported, not fixed
The Phase 1 skeleton gated on `min_trades = 3` with a TODO saying that was too
low. `gate.py` sets it to 10: three realised round-trips over ~250 daily bars is
noise, and the gate exists to reject noise. On real bars today the LLM-generated
crossover strategies essentially never clear it (most produce 0–4 trades, or a
negative return). That is the correct outcome and `cycle.run_cycle` reports it
as-is (every rejection carries the specific failing threshold). We did **not**
lower the bar to make the demo show a promotion (CLAUDE.md: never overstate).
The three thresholds live in one dict at the top of `gate.py`.
*Rejected:* keeping 3 (statistically meaningless); tuning thresholds per-run to
manufacture promotions.

### D22 — Two new tables in Phase 3: `orders` and `system_state`
`db.py` gains `orders` (every submission: strategy_id, run_id, broker_order_id,
symbol/qty/side, status, `submitted_via` CHECK('mcp','sdk'), `dry_run`, raw MCP
response) and `system_state` (key/value — the DB side of the kill switch). Both
are `CREATE TABLE IF NOT EXISTS` in the same idempotent script, same DB file.
D18 said Phase 4 needs no new DDL; that still holds — this DDL is for Phase 3's
execution path, not shadows.
*Rejected:* stuffing order records into `decisions` (different lifecycle, has a
broker id and a mutable status); a JSON blob column instead of a table.

### D23 — The MCP order path is a stdio subprocess driven by `fastmcp.Client`
`mcp_client.py` spawns `alpaca-mcp-server` (pip package, v2.3.0) as a subprocess
speaking MCP over stdio and calls its `place_stock_order` tool via
`fastmcp.Client` + `StdioTransport`. The CLAUDE.md STOP condition ("if the MCP
server is impractical to drive programmatically from our backend, stop and tell
me") was checked first: it is practical — `scripts/check_mcp.py` and
`scripts/run_cycle_demo.py` both complete real paper orders this way, and the
logs show every hop (`ROUTING ORDER VIA ALPACA MCP SERVER`, the subprocess line,
`invoking 'place_stock_order'`, `MCP ORDER ACCEPTED ... (submitted via MCP)`).
Notes: the package has no `__main__`, so we invoke `alpaca_mcp_server.cli:main`
directly; `ALPACA_PAPER_TRADE=true` is forced into the subprocess env so this
path can only reach the paper endpoint; `_call()` wraps the async round trip so
it is synchronously callable and monkeypatchable.
*Rejected:* the plain `mcp` SDK stdio client (hit "Connection closed" on
Windows where fastmcp's client worked); an HTTP-transport server (extra moving
part for no gain locally); direct `alpaca-py` order calls (D7 — the MCP hop is
the sponsor integration and must be visible in the demo).

### D24 — Risk checks: env-or-DB kill switch, plus position and notional ceilings
`risk.check(conn, notional=, dry_run=)` runs before every order in `cycle.py`,
with no code path around it. Order of checks: (1) kill switch — `KILL_SWITCH`
env var truthy **or** `system_state.kill_switch` truthy, either blocks
everything; (2) `MAX_CONCURRENT_POSITIONS = 3` — counts distinct strategies with
a non-terminal `orders` row; (3) `MAX_NOTIONAL_PER_POSITION = 2000` — a sanity
ceiling on top of the fixed $1000 notional. Every block is logged at WARNING
("ORDER BLOCKED — <reason>") and the reason is persisted (`orders.status =
'blocked'`, reason in `raw_response`). `DRY_RUN` (explicit arg or env var) is
surfaced on the result and honoured by `mcp_client` (no subprocess, no order),
but does not itself block — a dry run passes every check so the logs show what a
live run would do.
*Rejected:* a kill switch that is only an env var (a dashboard button in Phase 6
needs the DB flag); blocking on DRY_RUN (hides whether the rest of the path is
healthy).

### D25 — `data.py` now pins `feed=IEX`; generator prompt spans fast periods
Two small fixes surfaced by running the cycle live for the first time.
(1) `data._fetch_from_alpaca` now passes `feed=DataFeed.IEX`. Without it the
free-tier account 403s ("subscription does not permit querying recent SIP data")
as soon as the requested window reaches the present — this is the IEX-only
constraint already noted in D5, now enforced in code. Bug fix, not a refactor.
(2) `generator._user_message` now asks for some strategies with SMA/EMA periods
in the 2–15 range, not only slow 50/100/200 ones, so the candidate set actually
spans the trade-frequency axis the gate measures. The gate is unchanged.
*Rejected:* buffering `end` to yesterday instead of setting the feed (treats a
symptom); leaving the prompt alone (every candidate trivially fails `min_trades`
for a reason unrelated to strategy quality).

### D26 — `cycle.run_cycle` is the programmatic entry point; generation is injectable
`run_cycle(*, symbols, n, dry_run, conn, db_path, generate_fn, ...)` is a plain
function with no `__main__`-only logic — Phase 5 will call it from a FastAPI
handler. `conn` lets a caller (and the tests) reuse a connection; `generate_fn`
lets a caller substitute strategy generation (tests inject a fake; the demo
script wraps the real generator and appends a few hand-written fast-crossover
candidates so a full run — including a real MCP order — is demonstrable on a day
when the LLM's own output all fails the gate). Seeded candidates go through the
identical gate and risk path as any other; nothing is bypassed.
*Rejected:* a `run_cycle` that owns its DB connection and calls `generator`
directly (untestable without the network; not reusable from an HTTP handler).
