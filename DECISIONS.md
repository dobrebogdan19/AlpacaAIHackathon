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
