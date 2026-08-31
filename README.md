# Self-auditing trading agent

**Live demo: https://alpaca-self-audit.onrender.com/**
(Render free tier — the first load after an idle period takes ~30–60s to wake.)

An autonomous trading agent on Alpaca **paper** that audits its own decisions.
It generates candidate strategies with an LLM (as grammar-constrained data,
never code), backtests each on historical daily bars with a hand-written replay
engine, promotes only those that clear a fixed gate, and routes the survivors to
paper orders **through the Alpaca MCP server**. The signal comes from the
underlying equity; the *expression* is an options contract — when a promoted
strategy fires the agent buys a call on that underlying, not shares (see
[Options](#options) and [DECISIONS.md](DECISIONS.md) D48). What makes it different from a
typical strategy bot: it keeps tracking every candidate it *rejected* as a
shadow portfolio, so it can measure whether its own promotion gate actually
selects anything — and when a shadow beats a live strategy on forward data, it
retires the live one and writes a post-mortem. The claim is narrow and
deliberate: the agent **revises its decisions when evidence contradicts them**.
It does not learn, predict better over time, or generate alpha.

Built for the Alpaca AI Trading Agents Hackathon (lablab.ai).

---

## The loop

`generate → backtest → gate → execute via Alpaca MCP → shadow-track rejections → retire when evidence contradicts`

1. **Generate** — an LLM emits strategies as *data* in a closed grammar (fixed
   indicators and operators), validated by Pydantic. It never writes Python.
   (`generator.py`, `schema.py`)
2. **Backtest** — a plain hand-written replay loop, no backtesting library.
   Decide on bar N's close, fill at bar N+1's open. (`engine.py`)
3. **Gate** — three thresholds in one dict (min total return, max drawdown, min
   trades). Every candidate is stored with the exact numeric reason it passed or
   failed. (`gate.py`)
4. **Execute** — each promoted strategy produces a paper order through the Alpaca
   MCP server. The signal is about the underlying; the expression is a single-leg
   long **call** — `options.select_contract` picks one by explicit rules (30–45
   DTE, ~3% out of the money, a two-sided quote), records why, and the order goes
   out as a limit at the ask. Risk checks (kill switch, position caps, plus an
   option contract cap and a total-premium-at-risk cap) run before every order
   with no path around them. (`options.py`, `mcp_client.py`, `risk.py`)
5. **Shadow-track** — every rejected candidate is replayed forward from its
   decision date as a shadow portfolio. The selection-bias check compares the
   mean forward return of promoted vs rejected candidates: one number, reported
   with its sample size. (`regret.py`)
6. **Retire** — when a shadow beats a live strategy forward by a set margin, over
   a long-enough window, with the live strategy also losing *and the shadow
   having actually traded forward*, the live one is retired, the shadow promoted,
   any held position closed via MCP, and an LLM post-mortem (fed only the real
   numbers) is stored. On the committed seed no case clears all four conditions
   (see the limitations section). (`retire.py`, `postmortem.py`)

The whole loop runs **autonomously** — see below.

---

## Autonomous operation

`scheduler.py` is a background thread (started from the API when
`SCHEDULER_ENABLED` is set) that runs the agent on its own during US market
hours. Every 10 minutes it:

1. asks the Alpaca MCP server whether the market is open (`get_clock`) — closed →
   log the tick, do nothing;
2. runs a **position-management sweep** (`mgmt.py`): each open option position is
   priced from the broker and closed through MCP `close_position` if it hits an
   exit rule — one config dict: **+60% profit target, −50% stop, or ≤ 7 DTE**;
3. runs a full **entry cycle** if at least 3 hours have passed since the last one
   (daily-bar strategies only get new information once per day — a couple of
   cycles per trading day, not per minute).

Every tick — trading or not — writes a `scheduler_ticks` row, so the record shows
the agent *running*, not only when it acted. Render's free tier sleeps after
~15 min idle and a background thread is not traffic, so the scheduler stops when
the instance sleeps; an external uptime pinger on `/api/health` holds it awake
during market hours, and the first tick after any restart logs the gap honestly
rather than pretending a 24/7 loop. On startup `reconcile.py` syncs the local
`orders` table against the paper account so the risk caps reflect what is really
held. `GET /api/scheduler` serves the config and the tick log.

**Gate and risk caps for the live window.** Two deliberate, documented tradeoffs
for the judged window, both env-only — the strict defaults stay in `gate.py` /
`risk.py`, so the committed seed and all of the Phase-4 analysis are unchanged:

- The promotion gate's `min_trades` is calibrated at 10 for judging a strategy
  over a year of daily bars (rejecting noise); that filters out the
  medium-frequency strategies that could actually trade inside four days, so the
  deployed instance sets `GATE_MIN_TRADES=3` ([DECISIONS.md](DECISIONS.md) D50).
  `min_total_return` and `max_drawdown` are untouched.
- The Phase-3 risk caps (3 concurrent positions, $2,500 total option premium)
  were set for an equity project with **no P&L criterion**. P&L is the
  hackathon's first judging criterion and judges inspect the account directly, so
  the deployed instance sets `RISK_MAX_CONCURRENT_POSITIONS=8` and
  `RISK_MAX_OPTION_PREMIUM_AT_RISK=8000` (~8% of a $100k account), and widens the
  candidate universe (`DEFAULT_SYMBOLS` 8→24, `SCHEDULER_CYCLE_N` 4→8) so breadth
  drives activity on daily bars ([DECISIONS.md](DECISIONS.md) D53). The kill
  switch, the $2,000 notional ceiling and the 5-contracts-per-position cap are
  unchanged.

---

## How it uses Alpaca

- **MCP server for the agent's order path.** `mcp_client.py` spawns
  `alpaca-mcp-server` (v2.3.0) as a **stdio subprocess** and drives its
  `place_stock_order`, `place_option_order`, `get_option_chain` and
  `close_position` tools via `fastmcp.Client`. This is the agent-facing execution
  surface and it is what the demo shows — the logs print every hop. It runs
  inside the deployed **Linux container**: `GET /api/mcp-check` performs a real
  round trip and returns the live paper account. The subprocess environment is
  forced to `ALPACA_PAPER_TRADE=true`; the paper account is options level 3.
- **Market Data API for historical bars.** `data.py` uses `alpaca-py` to fetch
  daily bars (feed pinned to **IEX** — free tier), cached in SQLite so no range
  is fetched twice in a run. MCP adds nothing to bulk data pulls, so the SDK is
  used there.
- **Paper trading throughout.** No live endpoint is constructed anywhere in the
  codebase — not in code, not in examples, not in comments.

---

## Options

The hackathon requires strategies to incorporate options. This project keeps the
split explicit:

- **Signal** — unchanged. Every strategy is still evaluated on the *underlying
  equity's* daily bars. A promotion is a statement about the underlying.
- **Expression** — when a promoted strategy fires, `options.select_contract`
  chooses a single-leg long **call** on that underlying by the rules in one
  config dict (`SELECTION_RULES`): 30–45 DTE, strike ~3% out of the money, and a
  contract with a two-sided quote. The reasoning (spot, strike, moneyness, DTE,
  premium) is written into the order row. If nothing qualifies the cycle records
  a `skipped` order with the reason and moves on — it never crashes.
- **No option prices are backtested.** Alpaca's options history is short;
  modelling premium decay / IV would be days of work and exactly the kind of
  subtly-wrong result the rest of the project is built to avoid. The contract is
  chosen live at execution time.
- **Selection is by moneyness, not delta.** The free options feed carries Greeks
  and IV only inconsistently (SPY yes, AAPL no) and `open_interest` is always
  null, so a delta target and an OI floor are not dependable. Strike-vs-spot is
  always computable.
- **Limit orders at the ask.** Alpaca rejects options *market* orders outside
  market hours, and the demo must run at any hour, so every option order is a
  marketable limit — which also makes premium × 100 a true cap on the cost.
- **Risk.** `risk.check_option` adds a per-position contract cap (5) and a total
  premium-at-risk cap (default $2,500 across live option positions; the deployed
  instance widens this to $8,000 for the judged window, D53) on top of the kill
  switch and concurrent-position ceiling.
- `EXPRESSION = "options"` in `cycle.py` is the default; `"equity"` restores the
  original share path in one line. Both are tested.

`scripts/check_options.py` is the live probe — it lists the MCP option tools,
prints the account's options level, fetches a chain, and (with `--order`) places
one real 1-contract paper limit order.

---

## Correctness

The strongest technical claim here is that the backtest does not cheat.

- **Decisions on bar N's close, execution at bar N+1's open.** A strategy that
  decides and fills on the same bar is the most common way a backtest fabricates
  returns. The replay loop computes signals from bar N's close and fills at bar
  N+1's open, always.
- **No backtesting library.** No backtrader, vectorbt, zipline, bt. The replay
  engine is a plain loop we own and can explain under questioning.
- **The test that proves it:**
  `tests/test_engine.py::test_execution_uses_next_bar_open_not_decision_close`.
  Bars are built so bar N's close (20) and bar N+1's open (100) diverge sharply,
  then price stays flat with no exit signal. Correct next-open execution enters
  at 100 and ends **~0%**. Move the fill to the decision bar's close and the same
  strategy enters 5× cheaper and ends **~+400%**. The test asserts ~0%.
- **127 tests, all offline** — no network, no LLM, no MCP subprocess. `pytest -q`.
  (The option chain and every order path are faked in tests; the live checks live
  in `scripts/check_*.py`.)

---

## Architecture

| file | what it does |
|------|--------------|
| `schema.py` | the strategy grammar as Pydantic models — the closed vocabulary the LLM must emit |
| `generator.py` | LLM strategy generation, validation-retry loop, dedup by canonical hash |
| `seeds.py` | hand-written seed candidates (run through the identical gate) |
| `data.py` | daily-bar fetch via `alpaca-py` + SQLite range cache (fetches only missing ranges) |
| `engine.py` | the hand-written, lookahead-free replay engine |
| `gate.py` | the promotion gate — three thresholds (env-overridable for the live window, D50), a written reason for every verdict |
| `options.py` | contract selection — `SELECTION_RULES` (DTE + moneyness), records why; `NoContract` on a miss |
| `mcp_client.py` | the Alpaca MCP order path — stdio subprocess driven by `fastmcp`; stock + option orders + option chain |
| `risk.py` | pre-trade checks — kill switch, max concurrent positions, max notional; option contract + premium-at-risk caps (a held option counts until closed, D52) |
| `cycle.py` | `run_cycle()` — generate → backtest → gate → execute → persist; `EXPRESSION` picks option vs share |
| `scheduler.py` | the autonomous loop — market-hours ticks, entry cycles, the exit sweep; logs every tick |
| `mgmt.py` | option position management — `EXIT_RULES` (profit target / stop / DTE floor), closes via MCP |
| `reconcile.py` | startup sync of the `orders` table against the paper account, so the risk caps reflect reality |
| `regret.py` | the regret ledger — forward shadow replays, the selection-bias number |
| `retire.py` | the retirement rule + the numeric facts dict handed to the post-mortem |
| `postmortem.py` | LLM post-mortem, fed only numbers, plain-text fallback |
| `db.py` | SQLite persistence — one file for the whole system |
| `api.py` | FastAPI: read endpoints (pure `SELECT`) + `POST /api/cycle` + serves the dashboard |
| `static/index.html` | the dashboard — one static file, inline SVG, no build step |
| `scripts/seed.py` | builds the committed `seed.db` that a fresh deploy boots from |

---

## Run it locally

Requires Python 3.13, an Alpaca **paper** key, and an OpenAI key.

```bash
git clone https://github.com/dobrebogdan19/AlpacaAIHackathon.git
cd AlpacaAIHackathon

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then fill in the keys (see below)

pytest -q                        # 162 tests, all offline

uvicorn api:app --reload         # then open http://127.0.0.1:8000
```

Environment variables (full notes in [.env.example](.env.example)):

| var | required | purpose |
|-----|----------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | yes | paper account — historical bars + the MCP order path |
| `OPENAI_API_KEY` | yes | strategy generation (`gpt-4o-mini`) |
| `DB_PATH` | no | SQLite file location (default `bars_cache.db`) |
| `DRY_RUN` | no | truthy → no orders submitted; the rest of the path still runs and logs |
| `KILL_SWITCH` | no | truthy → every order blocked with a logged reason |
| `CYCLE_MIN_INTERVAL_S` | no | rate limit for `POST /api/cycle` (default 60) |
| `SCHEDULER_ENABLED` | no | truthy → the autonomous scheduler thread runs (unset locally) |
| `GATE_MIN_TRADES` | no | override the gate's `min_trades` (default 10; the deploy sets 3 for the live window, D50) |
| `RISK_MAX_CONCURRENT_POSITIONS` | no | override the concurrent-position cap (default 3; the deploy sets 8 for the judged window, D53) |
| `RISK_MAX_OPTION_PREMIUM_AT_RISK` | no | override the total option-premium cap in $ (default 2500; the deploy sets 8000, D53) |
| `SCHEDULER_CYCLE_N` | no | LLM candidates per entry cycle (default 4; the deploy sets 8) |
| `SKIP_SEED_BOOTSTRAP` | no | truthy → start from an empty DB instead of the committed `seed.db` |

Real keys go only in `.env`, which is gitignored. `.env.example` carries no
values.

Rebuild the seed database with `python scripts/seed.py` (or `--all-dry` for no
live orders).

---

## Limitations

Read this section. It is not boilerplate.

- **IEX data, not SIP.** The free tier serves a small fraction of total volume.
- **Daily bars only, roughly one year.** A strategy that clears the gate has
  cleared a low bar on a few hundred rows; nothing here is valid at intraday
  resolution.
- **Options are executed, not backtested.** The signal is validated on the
  underlying; the call contract is chosen live by fixed rules
  ([DECISIONS.md](DECISIONS.md) D48). There is no historical P&L for the option
  leg — Alpaca's options history is too short to model honestly. The free feed's
  option quotes are also thin and often stale, Greeks are inconsistent, and open
  interest is unavailable, so selection is moneyness-based and tradeability is
  just "a two-sided quote exists".
- **Forward tracking is a historical simulation.** The regret ledger runs "as of"
  a fixed past date (2026-06-17 on the committed seed) so genuinely unseen bars
  exist after it. This is **not** weeks of live shadow trading. The code, the
  API and the dashboard all label it as a simulation.
- **The selection-bias number has n = 8 vs n = 66.** On the committed seed the
  promoted-minus-rejected forward spread is **+3.85pp mean / +3.62pp median**
  (promoted +4.46%, n = 8; rejected +0.61%, n = 66), across 24 symbols. Small
  promoted sample, wide dispersion both ways — the gate **rejected the two
  biggest forward winners** (both MSFT, +26–29%). The number exists to show the
  instrument works — not to claim the gate selects signal. "Promoted" still means
  only "passed the gate on its window".
- **No retirement fires on the committed seed.** The retirement rule requires the
  winning shadow to have made ≥ 1 realised forward trade ([DECISIONS.md](DECISIONS.md)
  D45). The one candidate that cleared the return margin — a losing GOOG active
  vs a rejected GOOG shadow — is blocked because that shadow never traded (it
  "won" by sitting in cash). We present the rule and the fact that nothing met
  it: an honest null result, not a curated success story (D10).
- **Gate and risk caps loosened for the competition window.** The deployed
  instance runs with `GATE_MIN_TRADES=3` instead of the calibrated 10 (D50), and
  with the concurrent-position and option-premium caps widened to 8 and $8,000
  (~8% of a $100k account) plus a broader candidate universe (D53), so the agent
  actually builds a book inside the four-day P&L-judged window. Both are stated
  tradeoffs, not hidden ones — the strict defaults stay in `gate.py` / `risk.py`,
  the committed seed and all of the Phase-4 analysis are unchanged, and the kill
  switch, the $2,000 notional ceiling and the per-position contract cap are
  untouched.
- **Autonomy is real but not continuous.** On Render's free tier the scheduler
  stops whenever the instance sleeps. Gaps are logged, not papered over; a paid
  instance or a reliable external pinger would close them.
- **Paper trading only.** No live trading path exists in the codebase.
- **It does not learn.** No training, no weights, no online updates
  ([DECISIONS.md](DECISIONS.md) D9). It revises decisions when forward evidence
  contradicts them — that is the entire claim, and it is not overstated
  elsewhere, so do not read more into it here.

---

## What would come next with more time

- More cycles and a genuine forward window — the honest fix for every "sample too
  small" line above is time and volume, not code.
- Widen the grammar (more indicators, position sizing) once there is enough
  forward data to justify the added surface.
- A paid deploy tier with a persistent disk, so cycles triggered from the
  dashboard survive a spin-down.
