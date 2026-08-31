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

### D27 — Phase 5+6+7 built before Phase 4
The roadmap order is Phase 4 (regret ledger) then 5/6/7. We inverted it: a live
public URL is the non-negotiable deliverable (an undeployed project is not a
valid submission), and Phase 4 is more valuable built on top of a deployed,
observable system than as a prerequisite to it. Phase 4 is still fully scaffolded
in the schema (D18) — no rework is created by deferring it.
*Rejected:* building Phase 4 first and risking the deadline with nothing live.

### D28 — `api.py`: every GET reads stored rows; `POST /api/cycle` is the only write
The read endpoints (`/api/runs`, `/api/strategies`, `/api/orders`,
`/api/equity-curves`, `/api/health`) run pure `SELECT`s — rendering the dashboard
never needs a live market, a scheduler, or a network call (D6). `POST /api/cycle`
pre-creates the `runs` row so it can return the id immediately, then runs the
(slow: LLM + MCP) cycle in a FastAPI background task. It is single-flighted (a
`threading.Lock` + a `running` flag) and rate-limited to one per
`CYCLE_MIN_INTERVAL_S` (default 60s) in-process, so a visitor cannot spam the
OpenAI key or the paper account. `cycle.run_cycle` gained an optional `run_id`
param for this; passing `None` keeps the old behaviour (tests unchanged).
*Rejected:* a real task queue / Celery (scope — CLAUDE.md); computing anything on
a GET (D6); letting `run_cycle` create the run row and polling for "the latest"
(racy).

### D29 — The dashboard is one static HTML file, hand-drawn SVG, no build step
`static/index.html` is plain HTML + vanilla `fetch` + inline `<svg>` for the
equity curves. No framework, no npm, no bundler, no CDN chart library. The whole
UI is understandable from one screenshot: the decision log (every candidate,
PROMOTED/REJECTED, exact reason) is the centrepiece, with the equity-curve hero
above it and the orders table (showing `submitted_via='mcp'`) below.
*Rejected:* React/Vue (build step, CLAUDE.md scope); a CDN chart lib (a network
dependency for the one thing that must always render); server-side templating
(no gain over a static file for a read-only page).

### D30 — `seed.db` is a committed, separate SQLite file; the volume is seeded from it on first boot
`scripts/seed.py` runs a fixed 4-cycle sequence (LLM+fast-seeds live, slow-seeds
dry, plain-LLM dry, LLM+fast-seeds dry) against a clean `seed.db` and asserts the
result contains at least one promoting run and one all-rejected run. `seed.db` is
force-committed (a `!seed.db` exception to the `*.db` gitignore); the working
`bars_cache.db` stays untracked so local runs don't churn it. On the deployed
instance `api._bootstrap_db()` copies `seed.db` onto the persistent volume
(`DB_PATH`) if the volume is empty, so a fresh deploy shows a full dashboard
before anyone clicks anything. The step-1 live run submits real paper orders
through MCP, so the seeded orders table has genuine broker ids alongside
`blocked` rows (the position cap engaging — itself worth showing).
*Rejected:* committing `bars_cache.db` as-is (1.3 MB of bar cache, dirtied every
local run); generating the DB at container start (needs keys + network on boot,
slow, non-deterministic).

### D31 — `DB_PATH` env var selects the SQLite file; `db.connect()` reads it at call time
`db.py` and `data.py` both resolve `DB_PATH` (env var, else the repo
`bars_cache.db`) at import. `db.connect()` now takes `db_path=None` and falls
back to the *current* module global rather than binding the default at
definition time, so a test (or `scripts/seed.py`, which sets the env var before
importing) can repoint the whole system at another file.
*Rejected:* a config object / settings module (scope); leaving `connect`'s
default bound at def time (un-repointable without reimporting).

### D32 — Deploy target is Render, not Railway; free tier re-seeds on cold start
CLAUDE.md named Railway, but the operator's Railway account is on the free plan
with its one project slot used by an unrelated business app (`flowzy-v5`) that
must not be touched — `railway init` fails with "resource provision limit
exceeded". Render's free tier takes a Blueprint (`render.yaml`) and needs no
paid slot. Its trade-off: no persistent disk on free instances, so
`DB_PATH=/tmp/trading.db` is wiped on each ~15-min-idle spin-down and
`api._bootstrap_db()` re-copies the committed `seed.db` on every start. The
dashboard is therefore always populated (the seeded 4 runs); only cycles a
visitor triggers between spin-downs are lost. Acceptable for a demo — the record
that matters is seeded and committed. A paid instance + the `disk:` block in
`render.yaml` gives full persistence with no code change.
*Rejected:* Railway paid plan (cost, and not the operator's call to make);
deploying onto the existing `observant-quietude`/`flowzy-v5` service (would
clobber an unrelated production app); Fly.io / a VPS (more setup, no gain here).
*Supersedes the "Deployment: Railway" line in CLAUDE.md Stack.*
*Verified on deploy (2026-08-29): the Alpaca MCP stdio subprocess (D23) runs
fine in the Render Linux container — `/api/mcp-check` returned the real paper
account. The Windows-vs-Linux worry in D23 did not materialise.*

### D33 — On the deployed demo, seeded live positions saturate the risk cap
`seed.db` carries 3 non-terminal `accepted` MCP orders (seed run 1). `risk.py`'s
`MAX_CONCURRENT_POSITIONS = 3` counts non-terminal `orders` rows, so a fresh boot
starts already at the cap. A cycle triggered from the live "Run a new cycle"
button therefore promotes strategies but every resulting order is **blocked**
("max concurrent positions reached") — which is the risk control working, and is
visible in the decision log + orders table. This was left as-is: it is an honest
demonstration and blocking is correct given 3 open paper positions. To show a
fresh *accepted* order in a demo video instead, clear the 3 seeded positions
(close them in Alpaca and mark the `orders` rows terminal) or re-run
`scripts/seed.py --all-dry` so the seed carries no live orders.
*Rejected:* raising the cap for the demo (would misrepresent the control);
silently marking the seeded orders terminal (they really are open on the paper
account).
*Update (2026-08-29, Phase 4 reseed):* the stale pending orders were cancelled
via MCP (`cancel_all_orders`) before regenerating `seed.db`, so the account no
longer carries leftovers between seeds. Seed run 1 still submits 3 real orders,
so the cap behaviour above still holds on a fresh boot.

---

### D34 — `engine.run_backtest` gains `start_index` for forward tracking
Phase 4 tracks a rejected shadow forward from its decision date `T0`. To evaluate
a crossover/RSI on the first forward bar you need indicator history from *before*
`T0`. Slicing the bar list at `T0` instead would inject a spurious ~N-bar warm-up
(D16 makes an under-fed condition False), delaying every shadow's first signal
and distorting the exact number T4.5 reports. So `run_backtest(strategy, bars,
*, start_index=0)`: indicators still see `bars[0..n]`, but the trading loop, the
equity curve and every metric start at `start_index` — the strategy begins flat
there and trades forward. `start_index=0` is byte-identical to before (asserted
by `test_start_index_zero_is_identical_to_default`); D1 is untouched (decide on
close n, fill at open n+1; last-bar signal still dropped). This is the only
change to the engine.
*Rejected:* a bare `bars[split:]` slice (spurious warm-up, as above); an
`as_of`-date argument in the engine (it is otherwise date-agnostic — keeping it
index-based keeps it pure; `regret.py` owns the date→index mapping).

### D35 — The as-of date is set to leave a fixed forward-bar count, not searched
The regret ledger runs "as of" a past date `T0` so that genuinely unseen bars
exist after it. `T0` is chosen mechanically (`regret._pick_as_of`): the date of
the bar sitting `FORWARD_BARS_TARGET = 50` positions from the end of the longest
cached series. 50 is `retire.min_forward_bars` (40, ~2 trading months) plus a
25% margin, so a retirement *can* fire on a long-enough window — whether one does
is left to the data. On the committed seed (`seed.db`, generated 2026-08-29) this
resolves to **2026-06-17** and one retirement fired; the earlier run against the
pre-Phase-4 seed produced none. Both were kept as they came.
*Rejected, explicitly:* scanning candidate `T0` dates for one that produces a
retirement or a flattering selection-bias number. The instruction was that a
system which correctly declines to churn is a valid result, so the date is fixed
on a principled basis and the outcome is reported as-is.
*Note:* this is a **historical simulation of forward tracking**, not weeks of
live results. `regret.py`, `runs.as_of`, the `/api/shadow-curves` payload
(`simulation: true`) and the dashboard all say so; nothing presents it as a
live track record.

### D36 — Retirement rule: 5pp forward margin, ≥40-bar window, active also losing
`retire.RETIREMENT_POLICY` — three knobs, one dict, mirroring `gate.py`:
* `min_forward_bars = 40` — ~2 trading months. Below this, daily-bar forward
  performance is one or two trades' worth of luck, not evidence.
* `min_outperformance_pct = 5.0` — the shadow must beat the active by more than
  5 percentage points of forward total return. Smaller gaps are noise.
* `active_max_forward_return_pct = 0.0` — an **absolute** condition: the active
  strategy's own forward return must be ≤ 0. A profitable active is never
  retired just because a shadow did better — that would churn on noise. The
  claim (D9) is that the agent revises a decision when evidence *contradicts*
  it, and "the strategy we ran lost money while one we rejected did better" is
  exactly that.
Shadows are matched to actives **by symbol** — a forward-return comparison only
controls for the instrument when the instrument is the same. Actives are
considered worst-forward-first, so a shadow that could retire several is spent
on the biggest regret.
*Rejected:* no absolute condition (churns actives that are doing fine);
cross-symbol comparison (not a like-for-like alternative to the capital
decision); a drawdown- or Sharpe-based margin (total return is what the gate
and the shadow curves already speak in — keep one axis).

### D37 — The post-mortem LLM is fed only a numbers dict, never prose
`postmortem.generate_postmortem(facts)` where `facts` (built by `retire._facts`)
is strictly numeric: both strategies' in-sample and forward metrics, the window,
the policy. The system prompt forbids inventing market events, causes, price
moves or any narrative not derivable from those numbers, and asks only for a
contrast of in-sample vs forward plus what the gate missed. `_call_llm` is the
single network seam (monkeypatched in tests). If it fails or returns empty,
`_fallback_text` renders the same facts plainly — a retirement is never left
without an explanation. Stored in the new `postmortems` table with its
`facts_json` alongside, so a reviewer can check the prose against its inputs.
*Rejected:* passing the strategy rationales or names-with-adjectives to the model
(invites storytelling); no fallback (a flaky API key would break `seed.py`).

### D38 — Selection-bias is computed from stored forward backtests, reported with n
T4.5's headline: mean forward `total_return_pct` of as-of-**promoted** minus
as-of-**rejected** candidates, over the latest as-of run. `regret.selection_bias`
computes it from the in-memory records; `GET /api/selection-bias` recomputes it
from stored rows (`decisions` join `backtests WHERE kind='forward'`) so the
dashboard needs nothing live (D6). The sample size (`n_promoted`, `n_rejected`)
is returned and shown next to the number, every time — with four cycles of
candidates it is small, and saying so is part of the honesty (D10). On the
committed seed: **+2.22pp** (promoted +2.85%, n=4; rejected +0.63%, n=15).
Reported as-is whatever it says.

### D39 — Phase 4 DDL: `backtests.kind` / `backtests.as_of`, `runs.as_of`, `postmortems`
D18 said Phase 4 needed no migration. That held for the core shadow mechanic (a
forward replay is still just a `backtests` row) but not for two genuinely new
things, so — as D22 did for Phase 3's `orders` / `system_state` — `db.py` grew:
* `backtests.kind` ∈ {`primary`, `insample`, `forward`} and `backtests.as_of` —
  the split marker the selection-bias query needs;
* `runs.as_of` — flags a run as an as-of forward-tracking simulation;
* table `postmortems` (T4.4).
`CREATE TABLE IF NOT EXISTS` cannot add a column to an existing table, so the
three `ALTER`s run through `_apply_migrations`, guarded against `PRAGMA
table_info` — idempotent, and applied to the committed `seed.db` when it is
re-seeded onto the Render volume (D30). The read endpoints that predate Phase 4
(`/api/equity-curves`, `/api/strategies`, `/api/runs/{id}`) now filter
`kind='primary'` (or prefer it) so their meaning is unchanged.
*Rejected:* a separate `shadow_evals` table (the task wants forward runs
preserved *as backtests rows*); inferring `kind` from `bars_start` vs `as_of`
(fragile); a config/settings module for the new knobs (scope — they live in
`regret.py` / `retire.py` module constants like every other knob).

### D40 — The regret ledger reuses every stored strategy; it does not generate afresh
`regret.run_regret_ledger` evaluates the strategies already in `strategies`
(from the seed's four cycles) rather than asking the LLM for a new as-of batch.
Reason: it gives a larger, already-persisted sample for the selection-bias
number at no API cost, and the point of the ledger is to re-judge *decisions the
system actually made*, not hypothetical ones. `seed.py` runs it once, dry, after
the cycles. A retired strategy that held a real seed-run paper position is
"closed" on the dry path (an `orders` sell row, status `dry_run`) — the ledger
is a simulation and must not place or cancel live orders during seeding; the
real order stays as it is and the dashboard's simulation banner covers the
discrepancy.
*Rejected:* generating a fresh as-of batch (API cost, smaller sample, and it
answers a less interesting question); running the ledger live in `seed.py`
(a historical replay should not touch the broker).


### D41 — Dashboard is presentation-only reordered; machine reasons stay in the store

The dashboard opened with the selection-bias number and led with a full-height
decision log. Reordered to title + lede -> retirement case study -> selection
bias -> forward-tracking hero -> decision log -> orders -> run button: a concrete
retirement story lands before any statistic, and the selection-bias number only
means something once the reader knows the gate. Added a 2-paragraph lede so a
judge can understand the project without scrolling. Decision log now shows the
most recent run expanded with earlier runs behind a plain `<details>` toggle —
the full record stays on the page.

Verdict reasons are humanised **in `static/index.html` only** — `gate.py` still
stores the precise machine string ("all thresholds passed: total return 19.03%
>= 0.00%, ...") and the API still returns it verbatim; a JS transform rewrites
it for display ("Passed. +19.0% return, 8.9% max drawdown, 23 trades."), keeps
every number, drops operators/thresholds, and strips the "as-of DATE gate:"
row prefix (the run header states the as-of once). Unrecognised strings pass
through unchanged. The selection-bias caption was rewritten so the honest
reading leads: n is far too small to conclude anything; the claim is that the
instrument exists, not that the gate is proven to select signal (D9/D10).

*Rejected:* changing the stored reason format or adding a display field to the
API (the constraint was presentation-only); a JS framework or build step for the
collapse (a native `<details>` does it).


### D42 — Dashboard visual pass: type scale, one accent, framed data, case-file card

`static/index.html` styling only — no markup logic, no API, no copy, no JS
behaviour changed (the humanise transform and every `id`/class the JS emits are
untouched). What changed: (1) a real type scale — one system stack, a clamped
display title that dominates, section headings demoted to tracked accent
eyebrows over a hairline rule; (2) section boxes removed, replaced by 60px
vertical rhythm so ideas separate without reading; (3) a single navy accent
(`#1a4f8a`, matching the chart's existing pick colour) used only for headings,
rules, links, the button and focus rings — green/red/amber now appear *only* on
verdict pills; (4) tables get tabular figures, right-aligned numeric columns, a
fixed-width centred verdict column, row-hover, and each sits in its own
rounded frame that scrolls horizontally on mobile instead of overflowing the
page; (5) the retirement case study is the one elevated card — soft shadow,
accent spine, larger heading — so it reads as "the story"; (6) the
selection-bias figure is now neutral ink (it is an instrument, not a verdict —
green/red would misread it), its caption promoted to 15px medium and the honest
"too small to conclude" note made body-legible, inverting the old
shout/whisper.
*Rejected:* a webfont for display personality (no-dependency constraint — the
concept is instead "sans for the agent's prose, mono for its evidence");
keeping uniform section cards (no hierarchy); recolouring the chart lines in JS
(logic left untouched; the primary pick line is already the accent).


### D43 — Hero chart: one min–max band for the shadows, direct-labelled pick lines

`static/index.html` only, `heroSVG` rewritten (no API/logic/copy change beyond
the chart's own legend + the one now-stale clause in its caption). The old chart
drew 19 near-flat lines indexed by position in a 340px box — spaghetti, no
orientation. Now: (1) the ~15 rejected shadows collapse to a single shaded
min–max envelope (grey, plotted by real date); (2) the promoted picks draw on
top as 3px lines from the existing accent set (verdict green/red still excluded);
(3) viewBox is 960×500 so the shapes have vertical room; (4) added a marked zero
baseline, "nice"-stepped percent gridlines with labels, an as-of vertical marker
at the forward-tracking start, and four x-axis date ticks; (5) each pick is
labelled at its right-hand endpoint (collision-nudged) instead of in a legend
below — the legend keeps only a one-line band caption; (6) the retired pick is
dashed, so the retirement shows in the chart, not only the case-study card.
All still inline SVG, no library.

Also fixed a regression from D42: the decision-log Reason column was clipping
under auto table-layout because the Backtest column's `white-space:nowrap` ate
the width. The table is now `table-layout:fixed` with explicit column widths
(Reason takes the remainder and wraps freely) and `min-width:660px` so it
scrolls rather than crushes on a phone.
*Rejected:* keeping individual shadow lines but thinning them (still unreadable);
a median/quartile line inside the band (extra dashed line competes with the
"dashed = retired" signal — the brief asked for one envelope); a JS charting
lib (constraint).


### D44 — Wider candidate batch for a larger selection-bias sample; batch size fixed up front

D38's selection-bias number was computed over only the 19 strategies the four
seed cycles produced, across 7 symbols — n=4 promoted vs n=15 rejected, too small
to read into. `scripts/seed.py` now runs one extra step before the regret ledger:
`_generate_wider_batch` asks the LLM for a **fixed 3 candidates per symbol across
a fixed 24-symbol set** (72 requested), persisted as ordinary `source='llm'`
candidates — no cycle, no backtest, no order, because the ledger re-runs the gate
as-of on every stored strategy anyway (D40), so they are judged on identical
terms to the cycles' output. The ledger's `as_of` is now **pinned** to
`2026-06-17` (the date the first principled run used, D35) rather than
auto-picked, so the wider batch and the originals split on the same date.

Batch size was committed before running and not tuned to an outcome (D35 / D10):
24 symbols chosen for liquidity + history depth, 3 each, one pass, ledger run
once, spread reported as-is. The seed was **not** re-run to chase a number.

Outcome on the regenerated seed (as of 2026-06-17, today 2026-08-30):
* **75 strategies evaluated** (72 requested collapsed to 56 new rows after dedup
  against the cycles' output + within-batch dups; + the 19 from the cycles − a
  few skipped for short in-sample history).
* **Selection-bias spread +4.64pp** — promoted averaged +5.66% forward (n=12),
  rejected +1.02% (n=63). The gap **did not shrink, zero, or go negative — it
  widened slightly** from +2.22pp. But: the promoted median is +2.74% vs a
  rejected median of 0.00% (many rejected candidates never trade forward), so the
  **median spread is only +2.74pp**; standard deviation is ~8pp on both sides;
  and the gate **rejected the two largest forward winners** (MSFT Fast EMA Entry
  +28.82%, MSFT Momentum Breakout +26.63%). The larger n does not turn this into
  evidence that the gate selects signal — it is still an instrument reading, not
  a result (D10).
* **Retirement changed.** Re-seeding regenerates the four cycles' LLM output, so
  the earlier `AMZN Momentum and ATR → AMZN Volume Surge` case no longer exists.
  The new run retires **`GOOG Fast Entry`** (as-of promoted on a 114% in-sample
  return; forward −5.42%, having bought GOOG and held an unexited position down)
  in favour of the rejected shadow **`GOOG Slow Trend Following`** (+0.00% — it
  never fired a signal). The rule fires honestly (margin 5.42pp > 5.00pp, active
  ≤ 0), but this is a **thinner case** than the AMZN one: the winning shadow is
  inert and both GOOG strategies made 0 realised forward trades. It still
  illustrates the claim (D9) — a promoted strategy lost money, a rejected
  alternative did not — but it is a weaker demo centrepiece. Left as-is; the
  retirement rule was not changed to exclude do-nothing shadows (that is a
  separate decision if we want it).
*Rejected:* running the wider candidates through full cycles (would add ~24 runs
to the decision log for no gain — the ledger re-judges them regardless); leaving
`as_of` auto-picked (today advancing would drift the date and break comparability
with the first run); re-seeding until the retirement looked stronger (exactly the
selection bias this project measures — D35).


### D45 — Retirement requires the winning shadow to have actually traded forward

D44's retirement (`GOOG Fast Entry` → `GOOG Slow Trend Following`) had an inert
winner: the shadow that "beat" the losing active made **zero** realised trades,
in-sample or forward. It won by sitting in cash. That is a technicality, not a
demonstrable replacement — asked "what did the new strategy do?", the honest
answer is "nothing", and the case collapses under a judge's question.

`retire.RETIREMENT_POLICY` gains a fourth knob, `min_shadow_forward_trades = 1`:
a rejected shadow is only eligible to retire an active if it made at least that
many realised trades in the forward window. `ShadowRecord.forward_trades` reads
`forward_metrics["num_trades"]` (realised round-trips only, per D11 — a still-open
position does not count). The retirement `reason` string now states the winner's
forward trade count. Two tests added (`test_retire.py`): an inert shadow does not
retire; given an inert and a trading shadow, the trading one is used. 94 tests
pass (was 92).

**Outcome on the re-seed** (as of 2026-06-17, same 24×3 batch procedure as D44,
run exactly once — the batch was *not* searched for a better case):
* 74 strategies evaluated. **No retirement fires.** The only case clearing the
  return-margin + active-losing conditions: active `GOOG Momentum Breakout`
  (as-of promoted; in-sample +17.07% / 18 trades / 18.4% DD; forward **−7.60%** /
  5 trades over 50 bars) vs rejected shadow `GOOG RSI Overbought` (in-sample
  +0.00% / 0 trades — rejected as-of for "0 realised trades"; forward **+0.00%
  / 0 trades**). Margin +7.6pp > 5pp, active < 0 — but blocked by the new
  ≥1-forward-trade condition. The other losing active (`Momentum and Volume
  Surge`, V, −2.80%) had no same-symbol shadow beating it by 5pp.
* Selection bias: **+3.85pp mean, +3.62pp median** — promoted +4.46% (n=8),
  rejected +0.61% (n=66). The gate rejected the two largest forward winners:
  `MSFT Fast EMA Crossover` +28.82% and `MSFT RSI Overbought Oversold` +26.01%
  (both 1 forward trade). Still not evidence the gate selects signal.

**Pitch stance (D10):** present the strict rule and the null result. "We tightened
the retirement bar so a do-nothing shadow can't trigger it, and on our seed
nothing clears the stricter bar" is more credible than a retirement we would have
to caveat on stage. The retirement *mechanism* is fully built and tested; whether
a given dataset contains a qualifying case is left to the data (D35).

*Note:* re-seeding re-rolls all LLM output, so these strategies differ from
D44's. On this run the plain-LLM dry step (seed cycle 3) returned no valid
candidates — a transient model failure — leaving an empty run 3 in the decision
log. Cosmetic only; the seed's promote/reject guarantees still hold. Not
re-rolled to fix it (would be indistinguishable from searching for a nicer
selection-bias number).
*Rejected:* keeping the 0-trade shadow eligible (D44's problem); requiring the
shadow to also clear the *full* promotion gate forward (too strict — the point is
that a rejected candidate did better, not that it was secretly great); searching
seeds/dates for one that yields a retirement under the strict rule (D35 — the
exact bias this project exists to measure).


### D46 — `calibrate.py`: the gate proposes new thresholds from forward evidence; it never applies them

The regret ledger measures that the gate rejected the two biggest forward
winners (D45) and did nothing with it. `calibrate.py` closes that loop: it reads
every as-of decision of the latest regret run straight from storage (the stored
`insample` metrics = what the gate saw, the stored `forward` metrics = what
happened — no bars fetched, no backtest re-run), and grid-searches the three
`gate.py` thresholds for the combination that would have maximised the mean
forward return of the promoted set.

Design choices a reviewer might question:

* **Deterministic search.** `GRID` is three explicit ordered lists
  (`min_total_return_pct` 0–30 by 5, `max_drawdown_pct` 10–50, `min_trades`
  0–20); the nested loop keeps the first combination that *strictly* beats the
  best so far, so the result never depends on dict/set ordering. Exhaustive
  (~440 combos), no randomness, no early stop.
* **`MIN_PROMOTED = 5`.** A promoted set smaller than this makes the mean one or
  two strategies' luck — the same noise argument that puts `min_trades` at 10.
  Fixed up front; the holdout verdict is identical for 3 or 8 (checked).
* **Holdout.** Candidates are sorted by strategy id and every 3rd is held out
  (`HOLDOUT_EVERY = 3`, ~⅓). Calibrate on the other ⅔, then score BOTH the
  current and the train-calibrated thresholds on the held-out candidates — one
  number that is not fitted. The record reports the train improvement, the
  holdout improvement, and `improvement_survival_fraction` = holdout ÷ train.
* **Verdict, not just a number.** `no-improvement` (search beat nothing —
  `proposed` is reported as the *current* thresholds unchanged, not an arbitrary
  grid corner), `does-not-survive-holdout` (fitted gain reverses or barely
  survives out of sample — the D10 case), or `survives-holdout` (partial
  survival; still "candidate change, human must edit gate.py"). Every verdict
  except a genuine survival recommends keeping the current thresholds.
* **Never auto-applied.** `gate.py` is untouched by this module. The proposal is
  stored in a new `calibrations` table (plain `CREATE TABLE IF NOT EXISTS`, no
  migration — like `orders`/`postmortems`) with the full record JSON, and
  surfaced at `GET /api/calibration`. `applied` is a human-set marker. A system
  that silently retunes itself on fitted data is the exact failure mode this
  project exists to expose.

*Rejected:* applying the winning thresholds to `gate.py` automatically (the
whole point is that it must not); re-running the engine to get the forward
returns (the ledger already stored them — reading storage keeps it deterministic
and offline); a train/test split on time or symbol (interleaving by id keeps
both sides representative of the one as-of run); reporting the best fitted
combination with no holdout (that is the dishonest artefact the module exists to
refuse).


### D47 — On the frozen seed, the calibration does not survive the holdout (kept as-is)

`python calibrate.py --db-path seed.db` on the committed seed (regret run 5, as
of 2026-06-17, 74 candidates), run once, not searched:

* **Current gate:** promotes 8, mean forward return **+4.46%**.
* **Full-sample fit:** `min_total_return_pct` 0 → 10, `max_drawdown_pct` 25 → 15,
  `min_trades` unchanged. Promotes 5, mean forward **+8.66%** — a **+4.21pp**
  in-sample gain.
* **Holdout:** calibrated on 49 train candidates (→ 10 / 15 / 6), scored on 25
  held-out. Train improvement **+7.89pp**; holdout improvement **−0.30pp**.
  `improvement_survival_fraction ≈ −0.04` — the gain does not just shrink, it
  **reverses**.
* **Verdict:** `does-not-survive-holdout`. Recommendation: keep the current
  thresholds.

This is the correct and interesting result (D10): the tighter thresholds look
better only because they were fitted to the same forward returns they are scored
on; on candidates they were not fitted to, they are no better than the gate we
have. The calibration row is persisted into the committed `seed.db` (so the
deployed `/api/calibration` shows it) — this is a stored proposal record, not a
re-seed or an LLM re-roll, and `gate.py` is unchanged. The seed was not
regenerated and the grid/holdout parameters were not tuned to produce a nicer
verdict.

*Rejected:* presenting the +4.21pp full-sample number as "better thresholds"
(dishonest — it is in-sample optimism); widening the grid or changing the
holdout fraction until something survived (D35 — that is the selection bias this
project measures).


### D48 — Options are the *expression*; the signal still comes from the underlying

The hackathon requires strategies to incorporate options trading. We traded
equities only. Rather than rebuild the strategy grammar, the backtest, and the
gate around option contracts, the change is scoped to a single seam:

* **Signal — unchanged.** `engine.py` still evaluates every strategy on the
  underlying equity's daily bars. A promotion is still a statement about the
  underlying. The grammar, the gate thresholds, the regret ledger, the
  selection-bias number and the retirement rule are all untouched.
* **Expression — new.** When a promoted strategy fires, `cycle.py` no longer
  buys shares. `options.select_contract` picks a single-leg long **call** on
  that underlying and `mcp_client.submit_option_order` executes it through the
  Alpaca MCP server (D7 — MCP stays the order path). `EXPRESSION = "options"` in
  `cycle.py` is the default; `"equity"` restores the old share path in one line
  and both are tested.

**We do not backtest option prices.** Alpaca's options history is short and
modelling premium decay / IV would cost days we do not have. The backtest speaks
only in the underlying; the contract is chosen live at execution time. This is
stated the same way in the README.

**Selection is by moneyness, not delta.** `options.SELECTION_RULES` (one dict, like
`gate.py`): `contract_type=call`, `dte_min/max = 30/45`, `target_moneyness = 1.03`
(≈3% OTM — a directional bullish expression with convexity and defined risk),
`moneyness_tolerance = 0.08`, `require_two_sided_quote = True`,
`max_quote_spread_pct = 60`. The free / paper options feed carries **no Greeks or
IV on many underlyings** (SPY has them, AAPL did not — it is inconsistent) and
`open_interest` is always null, so a delta target and an OI floor are not
dependable. Moneyness (strike vs. the underlying's last close) is always
computable and fully explainable. Tradeability is proxied by "a two-sided quote
exists". The contract, the spot, the moneyness, the DTE and the premium are all
written into the `orders` row's `selection_reason` — this project logs its
reasoning everywhere and options are no exception. No suitable contract is
recorded as an `orders` row with `status='skipped'` and the reason; it never
crashes the cycle.

**Limit orders, not market.** Alpaca rejects options *market* orders outside
market hours (HTTP 422, code 42210000), and the demo must work at any hour
(CLAUDE.md). Every option order is a **limit at the contract's ask** — marketable
when the market opens, and it makes premium-at-risk (`ask * 100`) a true ceiling.

**Risk (`risk.check_option`).** Two option-specific ceilings alongside the
existing kill switch / concurrent-position checks: `MAX_OPTION_CONTRACTS_PER_POSITION
= 5` and `MAX_TOTAL_OPTION_PREMIUM_AT_RISK = $2,500` (summed over live BUY option
orders — a long option can expire worthless, so the premium paid is the whole
risk). `FIXED_OPTION_CONTRACTS = 1` per position (fixed size, like the fixed
notional). Kill switch and `DRY_RUN` apply unchanged.

**Persistence.** `orders` gains `asset_class` ('equity' default so every existing
row and the committed seed stay correct), `contract_symbol`, `underlying`,
`strike`, `expiry`, `premium`, `selection_reason` — guarded `ALTER`s in
`_apply_migrations` (the D22 / D39 pattern). `status='skipped'` needs no DDL
(the column has no CHECK).

**Confirmed before building:** the MCP server exposes `place_option_order`
(single- and multi-leg), `get_option_chain`, `get_option_contracts`,
`get_option_snapshot`, `close_position` (works on an OCC symbol) — all advertised
by default. The paper account is `options_trading_level: 3` (spreads allowed, no
approval flow) with `options_buying_power` ~$98.5k. `scripts/check_options.py`
placed a real 1-contract paper limit order (`AAPL261002C00330000`, id
`7f5cac80-…`, accepted, then cancelled) through the MCP path.

*Rejected:* rebuilding the grammar / backtest around options (weeks, not days —
and the interesting part of this project is the self-audit, not option
modelling); backtesting option prices on Alpaca's short history (would produce
exactly the kind of subtly-wrong result CLAUDE.md forbids); a delta target (not
available on the feed); market orders (rejected out of hours); defined-risk
verticals now (deferred — single-leg long calls first, per the task; the MCP
`place_option_order` `legs=` path is ready when it is time); replacing the equity
path outright (kept as a one-line toggle so the Phase 3 demo and its seed lineage
still run).


### D49 — Autonomous scheduler: 10-minute tick, entry cycle at most every 3 hours

The hackathon's first judging criterion is realised paper-account P&L, judged
over roughly four trading days (Mon 31 Aug – Thu 3 Sep 2026), and the agent must
run without a human clicking anything. `scheduler.py` is a daemon thread started
from `api.py`'s lifespan when `SCHEDULER_ENABLED` is truthy (unset locally and in
tests, so importing `api` never spawns a thread or touches MCP).

Cadence, and why:

* **Tick every `SCHEDULER_TICK_S` = 600 s (10 min).** Each tick asks the Alpaca
  MCP server `get_clock`; if the market is closed it logs the tick and stops.
* **Position-management sweep every tick.** Option P&L and DTE move intraday, and
  "an agent that only buys is not managing anything" — so the exit path
  (`mgmt.py`, D51) runs at the full tick cadence, not the entry cadence.
* **Full entry cycle at most every `SCHEDULER_ENTRY_INTERVAL_MIN` = 180 min.**
  The strategies are evaluated on **daily** bars, which only carry new
  information once per day (at the close). Re-running generate→gate intraday
  mostly re-judges the same bar. Two entry cycles per trading day covers the
  fresh daily bar plus one retry against a transient LLM/MCP failure, without
  spending the OpenAI budget or over-trading the paper account. ~2 entry cycles
  and ~39 sweeps per trading day.

**Free-tier sleep is handled honestly, not hidden.** Render's free instance
spins down after ~15 min with no inbound HTTP and a background thread is not
traffic, so the scheduler *will* stop when the instance sleeps. Mitigation: an
external uptime pinger on `/api/health` every ~10 min during market hours
(documented in `render.yaml` and the README). Whether or not that keeps it warm,
the first tick after any restart writes a `startup` row in the new
`scheduler_ticks` table recording the gap since the last tick — the record shows
real, possibly-interrupted operation rather than claiming a 24/7 loop. **Every**
tick writes a row (`skipped-market-closed` / `manage-only` / `entry-cycle` /
`error` / `startup`), so the log shows the agent running, not only when it
traded. Served read-only at `GET /api/scheduler`.

**Startup reconciliation.** On start the scheduler runs `reconcile.reconcile_orders`
(MCP `get_all_positions` + `get_orders`): any local `orders` row that still looks
open but is not backed by a live broker position/order is marked
`reconciled-closed` (a terminal status). Without this, a cold start that re-seeds
the DB from `/tmp` would carry stale rows that saturate `MAX_CONCURRENT_POSITIONS`
and block every new trade. A broker read failure changes nothing and is logged —
a stuck cap is safer than trading on a wrong picture of what we hold.

**The competition instance starts from an empty database** (`SKIP_SEED_BOOTSTRAP=1`)
against the fresh dedicated hackathon paper account, so there is no seed history
to reconcile away and the P&L judges see is entirely the agent's own. The
existing demo instance (with the committed `seed.db` and the Phase-4 analysis)
is unaffected — it just does not set `SCHEDULER_ENABLED`.

*Rejected:* a cron/APScheduler job (Render cron is a paid feature; an in-process
thread is simpler and needs no new dependency); entry cycles every tick (10 min
— pointless on daily bars, and burns the OpenAI key); a 90-minute entry interval
(~4/day — more visible activity but no more information, more paper orders);
pretending continuous operation and not logging the gap (dishonest — CLAUDE.md).


### D50 — Gate `min_trades` loosened 10 → 3 for the four-day live window (env, not a rewrite)

`gate.GATE_THRESHOLDS` is calibrated for judging a strategy over ~250 daily bars:
3 realised round-trips in a year is noise, so `min_trades` sits at 10 (D21). That
is the right number for the regret-ledger analysis and it stays the default —
`GATE_THRESHOLDS`, `calibrate.py`'s `current`, and the committed seed all still
read it, so D21/D44–D47 remain valid.

For the hackathon the constraint is different: the agent has ~four trading days
to open positions and produce P&L, and demanding 10 backtest trades filters out
exactly the medium-frequency strategies that could plausibly trade inside that
window. `gate.active_thresholds()` overlays three env vars
(`GATE_MIN_TRADES` / `GATE_MIN_TOTAL_RETURN_PCT` / `GATE_MAX_DRAWDOWN_PCT`) on the
strict dict; `evaluate()` uses it when no explicit thresholds are passed. The
competition instance sets **`GATE_MIN_TRADES=3` and nothing else** — 3 is the
Phase-1 floor, still requiring demonstrated repeat trading, not a single lucky
fire.

`min_total_return_pct` (0) and `max_drawdown_pct` (25) are **not** touched: they
are quality/risk controls on the backtest, and loosening them to chase returns is
explicitly out of bounds. The env hooks exist only so all three knobs stay in
one configurable place.

This is a stated tradeoff, not a hidden one: it goes in the write-up and the deck
as "we widened the gate for a short live window, here are the original values and
why they were right for a year of bars."

*Rejected:* editing `GATE_THRESHOLDS` in place (breaks the seed analysis and D21's
story); lowering `min_total_return`/`max_drawdown` too (chasing returns —
forbidden); a shorter backtest window instead (changes what every earlier number
means); tuning the value by running until the output looked good (D35 — the exact
bias this project measures; 3 was chosen up front as the pre-D21 floor).


### D51 — Option exit rules: +60% profit target / −50% stop / 7-DTE floor, one dict, closed via MCP

`mgmt.run_management_sweep` reads the real open positions from the Alpaca MCP
server every scheduler tick, prices each option leg from the position's own
`unrealized_plpc`, and closes the ones that hit a rule through MCP
`close_position` (same sponsor path as entries). `EXIT_RULES` is one dict, like
`gate.py` / `options.SELECTION_RULES`:

* **`profit_target_pct = 60`** — a long call is convex but decays; giving a solid
  gain back to theta is the common way these lose. +60% on the premium paid on a
  ~3% OTM 30–45 DTE call is a meaningful win to bank.
* **`stop_loss_pct = 50`** — max loss on a long call is 100% of premium; half of
  that is a natural line that still leaves room for ordinary noise.
* **`max_dte_to_hold = 7`** — inside a week, gamma/theta dominate and the
  underlying-signal thesis is out of time. Close (regardless of P&L) and let a
  fresh cycle re-enter at full DTE if the signal still holds. Also sidesteps
  expiration/assignment handling.

Every position looked at is logged (`held` / `closed` / `close-failed`), whether
or not it is closed — the sweep summary rides in each `scheduler_ticks` row. A
close inserts a `sell` `orders` row (`selection_reason` = the exit reason and the
P&L that triggered it) and flips the original BUY row to `closed` so the risk
caps (D52) stop counting it. The sweep never raises: a broker read failure comes
back as `SweepResult(error=…)` and the tick is still logged.

*Rejected:* trailing stops / partial scale-outs (more state, marginal on a
4-day horizon); a separate quote fetch per contract (the position object already
carries `unrealized_plpc`); a DTE floor low enough to ride into expiry week
(gamma risk, and the thesis has expired); leaving exits to a human (an agent that
only opens positions is not managing anything — the task).


### D52 — A filled option BUY still counts against the risk caps until the contract is closed

`risk.py` enforced `MAX_CONCURRENT_POSITIONS` and `MAX_TOTAL_OPTION_PREMIUM_AT_RISK`
by counting `orders` rows whose status is *non-terminal*. But `filled` is
terminal in that set — so once an option order filled, its premium stopped
counting and the position no longer counted toward the concurrent ceiling. For a
long option that is wrong: the premium is at risk, and the position is real,
until the contract leaves the account.

`risk._OPTION_CLOSED_STATUSES` is a separate, smaller set
(`canceled`/`expired`/`rejected`/`dry_run`/`blocked`/`error`/`skipped`/
`reconciled-closed`/`closed`). An option BUY row counts as an open position — for
both the premium sum and the concurrent-position count — until it reaches one of
those. The exit sweep sets `closed` (D51); the startup reconciliation sets
`reconciled-closed` (D49); expiry/cancellation come from the broker status. The
equity path is unchanged (a filled stock buy is still terminal, per the Phase-3
tests) — this only tightens the option ceilings, which the task requires stay
fully in force.

*Rejected:* making `filled` non-terminal globally (would change Phase-3 equity
semantics and break its tests); a separate `positions` table (the `orders` rows
plus the sweep/reconcile already carry the lifecycle); raising
`MAX_TOTAL_OPTION_PREMIUM_AT_RISK` so the leak did not matter (explicitly
forbidden — do not raise the premium cap to chase returns).


### D53 — Risk caps widened for the competition window (env, not a rewrite); supersedes the D52 note for this deployment

D52 said "do not raise the premium cap to chase returns", in the context of a
*leak* — a filled option that had stopped counting. That still stands: the leak
is fixed and the caps are enforced. This entry is a different thing — a
**deliberate, documented widening of the enforced ceilings for the four-day
judged window**, decided the same way and for the same reason as the D50 gate
change.

**Why.** `risk.py`'s caps (`MAX_CONCURRENT_POSITIONS = 3`,
`MAX_TOTAL_OPTION_PREMIUM_AT_RISK = $2,500`) were set in Phase 3 (D24) for an
equity project whose deliverable was a self-auditing decision loop with **no P&L
criterion**. The hackathon's *first* judging criterion is realised paper-account
P&L, judged by inspecting the account directly over ~four trading days. A cap
that idles the agent after three positions — on a $100k account, with a
concurrent-position ceiling built when the fixed notional was $1,000 — measures
nothing a judge is looking at. Three positions is not a risk posture for this
context; it is an accident of a different one.

**What moves (competition instance only):**

| cap | strict default (risk.py) | competition (render.yaml) | why |
|---|---|---|---|
| `MAX_CONCURRENT_POSITIONS` | 3 | **8** | enough breadth that the account shows an actual book |
| `MAX_TOTAL_OPTION_PREMIUM_AT_RISK` | $2,500 | **$8,000** | ~8% of a $100k account is the total long-option loss ceiling |
| `MAX_OPTION_CONTRACTS_PER_POSITION` | 5 | **5 (unchanged)** | per-position size is not the thing being loosened |

**What does not move:** the global kill switch (env + DB), `MAX_NOTIONAL_PER_POSITION`
= $2,000 on the equity path, `FIXED_OPTION_CONTRACTS` = 1, and the D52 rule that a
filled long option keeps counting until the contract leaves the account. 8
positions × ≤5 contracts is still bounded by the $8,000 premium sum, so a single
bad day cannot lose more than ~8% of the account to premium decay.

**Mechanism — identical to D50.** `risk.limit(name)` returns the module default
unless the matching `RISK_*` env var is set to a number (`RISK_MAX_CONCURRENT_POSITIONS`,
`RISK_MAX_OPTION_PREMIUM_AT_RISK`). `risk.py`'s constants are unchanged, so
`seed.db`, the regret-ledger analysis, `calibrate.py` and all 158 tests still read
the strict values; only the Render competition service overrides them. The
existing demo instance (committed `seed.db`, Phase-4 analysis) is untouched — it
sets none of these vars.

**Breadth, not frequency, is the activity lever (companion change).** On daily
bars a strategy's signal only changes once per day at the close; running the
entry cycle more often mostly re-judges the same bar (D49). So activity comes
from a *wider* candidate set, not a faster loop:
`generator.DEFAULT_SYMBOLS` 8 → 24 (large caps + major ETFs with tight option
markets: SPY QQQ IWM DIA / AAPL MSFT NVDA AMZN GOOGL META TSLA AMD AVGO NFLX /
JPM BAC GS / WMT COST KO DIS XOM CVX UNH), `seeds.SEED_STRATEGIES` 4 → 8, and
`SCHEDULER_CYCLE_N` 4 → 8 so the wider universe is actually sampled per cycle.
The tick cadence (10 min) and entry interval (3 h) are unchanged.

**This is a stated tradeoff, not a hidden one** — it goes in the write-up and the
deck next to D50: "we widened the risk caps and the candidate universe for a
four-day live window; here are the original values, why they were right for the
Phase-3 project, and why they were wrong for a P&L-judged demo."

*Rejected:* editing the constants in `risk.py` directly (breaks the seed lineage
and the tests, same reason D50 kept `GATE_THRESHOLDS` intact); moving
`MAX_NOTIONAL_PER_POSITION` or the per-position contract cap (position *size* is
not the constraint being wrong here); raising the caps far enough to never bind
(the point is a visible book with a real ceiling, not unlimited exposure);
increasing the tick frequency instead of the universe (no new information on
daily bars — D49).


### D54 — Dashboard reads the live paper account: `GET /api/account`, cached, last-known-on-failure

The hackathon's first judging criterion is realised paper-account P&L, judged by
inspecting the Alpaca account directly. Our own dashboard showed nothing about
account state — a real gap. `account.py` fills it: portfolio value, cash, total
P&L against the fixed `STARTING_BALANCE = 100_000.0`, and every open option
position with its unrealised P&L.

It is the **one** dashboard GET that touches the network. Every other read
renders from stored rows only (D6); this one cannot — the number has to be live
to match what a judge sees. It goes through the **MCP path** (`mcp_client.check_connection`
+ `list_positions`), not a direct SDK client, same as the order path.

To keep page loads and the uptime pinger off the MCP subprocess, the computed
snapshot is cached in the existing `system_state` kv table for
`ACCOUNT_CACHE_TTL_S` (default 45s), with a process lock so concurrent requests
collapse to one fetch. A failed live read serves the last cached snapshot with
`stale: true` and its timestamp; with no cache at all, `{"available": false}`.
The panel always shows a number or an honest "unavailable", never a 500.

*Rejected:* a direct `alpaca-py` client (D7 — the MCP path is the sponsor
integration and must be the one that moves money-adjacent data); a new SQLite
table for the cache (the `system_state` kv table already exists for exactly this
kind of small transient value); no cache (every dashboard load and every pinger
hit would spawn the MCP subprocess); computing P&L from stored order rows
instead of the live account (it would drift from the account a judge inspects —
the whole point).


### D55 — Plain-language activity summary: LLM over a facts dict of stored rows, cached hourly

The dashboard reads like an engineer's output — decision tables, metric strings,
order rows. `summary.py` adds a 3-5 sentence plain-English note near the top:
how many cycles ran, what was opened and the stated reason, what was closed and
why, what is held now.

Same discipline as the post-mortem writer (D37): the LLM is handed **only** a
facts dict built from real stored numbers (`build_facts` — runs, scheduler tick
counts, order rows joined to their strategy's rationale, retirements) and the
system prompt forbids inventing tickers, prices, dates, or performance claims
not in the data. `_fallback_text` renders the same facts plainly when the call
fails or no key is set, so the panel is never empty-because-of-an-error.

Provider is OpenAI `gpt-4o-mini`, one constant, as in `generator` / `postmortem`.
Generation costs a call (~10s observed) and the content barely moves between
loads, so the result is cached in `system_state` and regenerated only when the
cache is missing or older than `SUMMARY_MAX_AGE_S` (3600). The frontend fetches
it asynchronously alongside every other panel, so the slow first generation
never blocks the rest of the page.

*Rejected:* generating on every page load (wasteful and slow — the brief says
at most hourly); free-text narrative not tied to stored numbers (D25/D37 — the
one thing this project must not do is let the model narrate beyond the
evidence); a background regeneration thread (another moving part; a lazy
hourly refresh on read is enough).


### D56 — Empty as-of panels say the analysis ran on `seed.db`, not "no data yet"

The live competition instance starts from an empty database (`SKIP_SEED_BOOTSTRAP`,
D49). The regret-ledger study — selection-bias spread, shadow curves, the gate
calibration proposal, the one retirement it produced — lives in `seed.db` and
was run on a separate development dataset. On the live instance those four
panels have no data and never will: that account has days of history, not the
months the as-of split needs.

Showing "no data yet" would imply it is coming. Instead each empty panel now
states plainly that this is the live instance, that the as-of analysis was run
on `seed.db` in the repo, and that it is not reproduced here because it is not
this instance's data. One shared `devDataNote()` helper, per-panel wording.

This also removed the old `loadHero` fallback that drew plain backtest curves in
the forward-tracking section when no as-of run existed — on the live instance
that would have mislabelled ordinary in-sample curves as forward tracking.

*Rejected:* seeding the live instance with `seed.db` so the panels populate (D49
— the competition account trades clean, and the as-of analysis is a different
account's history); hiding the panels entirely (the analysis is real work and
part of the story — the honest move is to point at where it lives, not to erase
it); a generic "no data" string (reads as a bug or a pending state, not a
deliberate separation of datasets).


### D57 — Dashboard lede and top hierarchy: shorter copy, smaller title

The header lede had grown to a five-sentence paragraph that read like generated
marketing copy — rhetorical build-up before any concrete claim. Cut to three
plain sentences plus the "paper trading only" tag: what the agent does, why the
rejections matter, when it retires a strategy.

Proportion, not redesign — palette and typography unchanged. The `h1` clamp was
oversized against everything below it (max 1.95rem vs 0.875rem section headings);
brought to a 1.3–1.55rem clamp. Section `h2` nudged 0.875–0.9rem with tighter
tracking for a little more presence. Top rhythm tightened: header padding
26/22 → 22/20, `main` padding-top 34 → 24. `.acct-value` clamp lowered
(1.65 → 1.45rem max) so the incoming account panel sits below the title in the
hierarchy rather than rivalling it.

*Rejected:* recolouring or reweighting headings for presence (character change,
not proportion — out of scope); touching the `.bias-num` instrument readout
(a data figure deep in the page, not a heading).


### D58 -- Restart incident: broker becomes the source of truth for risk state

**What happened (2026-08-31).** The T6.4-T6.6 dashboard work (D54-D57) was
deployed at ~16:53 Bucharest, in a window the operator had approved, ~20 min
after the day's first entry cycle (16:32: 16 generated, 8 promoted, 8 orders, 7
filled, 6 option positions). The deploy restarted the Render free-tier instance.
The free tier has no persistent disk, so `DB_PATH=/tmp/trading.db` was wiped, and
`SKIP_SEED_BOOTSTRAP=1` meant it came back empty. The scheduler started, saw no
`scheduler_ticks` row for a prior entry cycle, concluded it had never traded, and
on its first market-open tick (~16:58) ran a **full second entry cycle**: 8 more
orders. Account went from 6 positions to 9.

**Root cause.** `risk.py` measured both ceilings -- max concurrent positions and
total option premium at risk -- by counting rows in the local `orders` table.
`reconcile.py` only ever flipped existing rows to closed; it never rebuilt rows
from the broker. So when the wipe emptied `orders`, `risk` saw **0 positions /
$0 premium** and passed all 8 promotions. `scheduler._entry_due` had the same
single-source weakness: the "last entry cycle" marker also lived only in the
wiped DB.

**What breached.** With the D53 competition caps (`RISK_MAX_CONCURRENT_POSITIONS=8`,
`RISK_MAX_OPTION_PREMIUM_AT_RISK=8000`):
- concurrent positions: **9** vs 8 allowed
- option premium at risk: **~$10,300** vs $8,000 allowed (the ~$4,900 from the
  first cycle was invisible to the check)
- portfolio at the time: ~$98,700, P&L ~-$1,300 (-1.3%); the drift since is
  market movement, not new trading.

The over-cap positions were **left in place** on purpose -- the honest record of
what happened is worth more than a tidied account.

**What changed.**
1. **Scheduler halted** (`SCHEDULER_ENABLED=0`, commit bfccefd) the moment the
   incident was understood. Stays off until the fix is verified on the deployed
   instance.
2. **`BROKER_TRUTH=1`** (new env flag, deploy-only; off in tests/local so the
   suite is unchanged). With it on:
   - `risk.py` computes both ceilings from the **broker** -- open option
     positions + working option orders via the MCP path -- unioned with local
     non-terminal rows so an order placed earlier in the *same* cycle still
     counts before the broker reports it. Result memoised ~20s so a cycle does
     one round trip, not one per candidate.
   - Fail-safe: if the broker read fails **and** there are no local rows to fall
     back on, `risk` blocks new entries. A wedged agent beats a blind one (same
     principle as `reconcile.py`'s existing "stuck cap is safer").
   - `reconcile.backfill_positions` inserts one `reconstructed` order row per
     open broker position that has no local row, built only from broker facts
     (contract, qty, cost basis), pointing at a single shared synthetic strategy
     (`dedup_key='__reconstructed__'`, `source='manual'`). The strategy rules and
     the gate reason are **not** invented -- `selection_reason` says so plainly,
     and a new `orders.reconstructed` flag lets the dashboard label the row.
   - `scheduler._entry_due` takes `max(local marker, latest broker order
     timestamp)` -- new `mcp_client.list_recent_orders()`. After a wipe the
     broker's own order history says "you traded 20 minutes ago", so no
     catch-up cycle.
   - Startup guard: `reconcile` logs a loud WARNING naming any broker position
     the local DB did not know about, then proceeds on broker truth.
   - `api._lifespan` runs the broker sync at boot when the scheduler is off, so
     the manual `POST /api/cycle` path is protected too.
3. **Dashboard** states plainly that run/decision history resets on every
   free-tier restart and only covers time since the instance last started, that
   positions and orders are read back from the broker and survive restarts, and
   labels reconstructed rows as such. `summary.py` folds reconstructed holdings
   into its facts and says when holdings were rebuilt (no more "holds nothing"
   while the account panel shows positions).

**Persistence, explicitly not done.** Render free-tier has no persistent disk;
its free Postgres expires 30 days after creation. Litestream to Cloudflare R2 or
Backblaze B2 was the free-code option but every such bucket now requires a
registered payment card. Decision: **do not pay, do not register a card, accept
that run/decision history resets on restart.** It is compensated where free --
positions and orders are reconstructed from the broker, and the dashboard is
honest about the rest. If a zero-account, zero-card way to persist the decision
log appears (e.g. committing periodic DB snapshots back to the repo), revisit.

*Rejected:* paid Render instance + disk (~$7.25/mo -- operator will not pay);
Litestream + R2/B2 (card required); trimming the over-cap positions to tidy the
account (the breach is part of the record); keeping `risk` on local rows and
only fixing `reconcile` (leaves `risk` blind for the seconds between boot and
the first successful broker read, and offers no fail-safe).


### D59 -- GitHub Actions DB snapshots: persist the history the broker can't give back

D58 makes a /tmp wipe *safe* (risk reads the broker; positions/orders are
reconstructed). It does not make it *lossless*: backtests, the decision log, run
history and scheduler ticks have no broker equivalent, so the equity chart and
most of the decision log reset on every restart -- the dashboard looks emptier
than the system is, which a judge would notice.

**Mechanism.**
- `GET /api/db-snapshot` -- `sqlite3.Connection.backup` takes a consistent copy
  even while the scheduler writes; `cache:*` rows in `system_state` are dropped;
  gzipped with `mtime=0` so an unchanged DB is byte-identical between calls.
- `.github/workflows/db-snapshot.yml` -- every ~30 min (and on demand) pulls that
  file, validates it (gzip + SQLite magic + opens + has a `runs` table), runs
  `.github/scripts/scan_snapshot.py` (fails the job on anything key-shaped in any
  text column), and force-pushes it to the orphan `snapshots` branch as a single
  commit. Public repo + `GITHUB_TOKEN` only -- no card, no external account.
- `api._restore_from_snapshot()` -- on boot, if the local DB has zero `runs`,
  fetch `https://raw.githubusercontent.com/<repo>/snapshots/trading.db.gz` and
  load it. Then `reconcile.sync_with_broker` (D58) corrects the position view on
  top. The restore runs before `seed.db` and before the `SKIP_SEED_BOOTSTRAP`
  early-return, so the competition instance gets its own history back, not the
  dev seed.

**Never overwrites newer data.** Restore is gated on `runs == 0`. The scheduler
writes a `startup` tick and reconcile writes rows within seconds of boot, so a
live DB is never "empty" -- the only time restore fires is immediately after a
wipe, when there is nothing to lose. A snapshot is at most ~30 min stale;
anything the agent did in that window and did not snapshot is genuinely gone (an
honest limit, stated on the dashboard).

**No secrets in the snapshot.** Credentials are env vars, never written to the
DB. Verified: the scan script greps every text value in every table against
Alpaca / OpenAI / AWS / GitHub / Slack / PEM key shapes and fails the job on a
hit; it is also a unit test (clean DB passes, planted `sk-proj-...` fails). The
DB's contents (orders with broker ids and raw MCP JSON, runs, decisions) are
already served by the read endpoints -- the snapshot is not a new disclosure.

**Autodeploy is not triggered.** Render autodeploys `main`; snapshots go to the
orphan `snapshots` branch, which it ignores. No restart, no scheduler halt.

*Rejected:* Render paid instance + disk / Litestream + R2 / Backblaze B2 (all
need a card, D58); committing snapshots to `main` (every 30 min -> an autodeploy
loop, or a fragile Render "ignored paths" dashboard setting); the app committing
its own snapshots via a PAT (a credential to manage; the workflow-pull path uses
the auto `GITHUB_TOKEN`); restoring always and merging (a stale snapshot would
clobber live rows -- restore-only-when-empty is the safe rule).

**Scope note.** CLAUDE.md says no CI pipelines. This is not CI (it runs no tests,
gates no merges) -- it is a data-persistence cron that happens to run on Actions
because that is the free scheduler we already have. Explicitly requested.

**Verified on the deployed instance 2026-08-31.** GET /api/db-snapshot returns a ~78 KB DB / ~3 KB gzip; the secret scan is clean (the script, plus a raw byte check that no .env value appears in the file). The snapshots branch was bootstrapped by hand once (no gh CLI locally); the workflow maintains it from there. A restart then loaded it -- scheduler_ticks came back with the pre-restart rows and startup logged "resumed after 181 min gap" instead of "first scheduler start" (a no-restore boot has exactly one tick). The runs == 0 gate means the first post-fix cycle's backtests and decisions persist from here; the equity chart fills once a cycle has run.

---

### D60 -- Management sweep gains a hard time stop: close after 2 days if no other rule hit

**The deadlock.** On 2026-08-31 the paper account held 9 open option positions
against a concurrency cap of 8 (`RISK_MAX_CONCURRENT_POSITIONS`, D53). All 9 were
long calls, all opened the same day within ~24 minutes, all underwater between
-11% and -36%. None had hit the three `EXIT_RULES`: +60% profit target, -50% stop,
or <=7 DTE (the contracts were 30-46 days out). So the sweep closed nothing, and
`risk.py` correctly blocked every new entry (9 >= 8). The scheduler kept working
-- the 18:26 cycle generated 16 candidates and promoted 4 -- but could not place a
single order. The agent was frozen until one of the 9 positions happened to cross
a threshold on its own, which could take days or expiry.

**Root cause.** Three things together: (1) a single bad batch -- one entry path
opened many *correlated* positions (index + mega-cap calls, all directional long)
that move together and are therefore all underwater together; (2) a hard
concurrency cap with no headroom once that batch fills every slot; (3) every exit
rule being price- or expiry-contingent, so a batch that just sits mildly
underwater satisfies none of them, indefinitely. Any one of the three alone is
survivable; all three is a deadlock.

**The fix.** A fourth entry in `EXIT_RULES` -- `max_calendar_days_to_hold` -- and
the sweep machinery to evaluate it:
- `run_management_sweep` reads the broker's order history once
  (`mcp_client.list_recent_orders`) and folds it to the *earliest* `filled_at`
  per OCC symbol (a contract built up over several fills keeps its first entry
  time, not the latest).
- `evaluate_exit` takes a `held_days` argument and a fourth branch, checked
  **only after** profit-target / stop-loss / DTE -- those keep precedence, so a
  position that is both stale and a winner closes on the profit reason.
- Holding age comes from the broker, never a local `orders` row, per D58. A
  `/tmp` wipe with a missed snapshot has `reconcile.backfill_positions`
  reconstruct rows with `created_at = boot time`, which would reset every age to
  zero; broker `filled_at` survives the wipe. Verified against the live account
  that `list_recent_orders` returns option BUYs with populated `filled_at`.
- If the age is unknown for a position -- no filled buy in history, unparseable
  timestamp, or the order-history read itself failed -- the rule **does not
  fire**: it is logged and skipped. A missing timestamp can never cause a close,
  so a boot with degraded broker data cannot mass-liquidate.

**N = 2 calendar days (48h since fill).** The entry interval is 3h and the thesis
is a daily-bar signal on the underlying; if a long call is still flat-to-underwater
two sessions after entry, the directional call has not worked and theta is now the
dominant term. Short enough to break a deadlock inside a single demo day, long
enough that an ordinary winner reaches +60% first. Not env-overridable -- this is a
correctness backstop, not a tuning knob.

**Known trade-off.** A time stop realizes losses that might have recovered: a long
call down 20% on day 2 can still finish green by expiry, and closing it locks the
loss and pays the spread. Accepted because the observed alternative is worse --
total paralysis, the agent unable to act on any new evidence for days. The regret
ledger (Phase 4) will show whether time-stopped positions tended to recover; if
that signal is strong, N is the knob to revisit.

**Mass close is safe.** All 9 current positions share an entry day, so the first
sweep past 48h closes all 9 in one pass. Each close is an independent
`mcp_client.close_position` call; a failure returns `OrderResult(ok=False)`,
increments `failed`, is logged, and the loop continues. The local buy row is
flipped to `closed` only for closes the broker accepted, so a partial failure
just leaves those positions for the next tick (10 min) to retry -- no rollback, no
all-or-nothing.

*Rejected:* raising the concurrency cap (delays the next deadlock, raises
exposure, does not fix the cause); a manual one-off close of the batch (recurs the
next time a cycle opens a correlated batch); a correlation/diversification check
at entry (the right long-term fix but a larger change to the entry path -- the
time stop is the safety net regardless); age from the local `orders.created_at`
(unreliable across a wipe, see above); date-boundary "calendar day" counting
(timezone-fragile; elapsed-48h is unambiguous and matches how the other rules
read).

Scope: `mgmt.py` + `tests/test_mgmt.py` only. `risk.py`, the entry cycle, the
scheduler, the MCP order path and the D58 reconciliation are untouched. One
deploy.
