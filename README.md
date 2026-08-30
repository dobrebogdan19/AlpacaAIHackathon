# Self-auditing trading agent

**Live demo: https://alpaca-self-audit.onrender.com/**
(Render free tier — the first load after an idle period takes ~30–60s to wake.)

An autonomous trading agent on Alpaca **paper** that audits its own decisions.
It generates candidate strategies with an LLM (as grammar-constrained data,
never code), backtests each on historical daily bars with a hand-written replay
engine, promotes only those that clear a fixed gate, and routes the survivors to
paper orders **through the Alpaca MCP server**. What makes it different from a
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
   MCP server. Risk checks (kill switch, position and notional caps) run before
   every order with no path around them. (`mcp_client.py`, `risk.py`)
5. **Shadow-track** — every rejected candidate is replayed forward from its
   decision date as a shadow portfolio. The selection-bias check compares the
   mean forward return of promoted vs rejected candidates: one number, reported
   with its sample size. (`regret.py`)
6. **Retire** — when a shadow beats a live strategy forward by a set margin, over
   a long-enough window, with the live strategy also losing, the live one is
   retired, the shadow promoted, any held position closed via MCP, and an LLM
   post-mortem (fed only the real numbers) is stored. (`retire.py`,
   `postmortem.py`)

---

## How it uses Alpaca

- **MCP server for the agent's order path.** `mcp_client.py` spawns
  `alpaca-mcp-server` (v2.3.0) as a **stdio subprocess** and drives its
  `place_stock_order` / `close_position` tools via `fastmcp.Client`. This is the
  agent-facing execution surface and it is what the demo shows — the logs print
  every hop. It runs inside the deployed **Linux container**: `GET
  /api/mcp-check` performs a real round trip and returns the live paper account.
  The subprocess environment is forced to `ALPACA_PAPER_TRADE=true`.
- **Market Data API for historical bars.** `data.py` uses `alpaca-py` to fetch
  daily bars (feed pinned to **IEX** — free tier), cached in SQLite so no range
  is fetched twice in a run. MCP adds nothing to bulk data pulls, so the SDK is
  used there.
- **Paper trading throughout.** No live endpoint is constructed anywhere in the
  codebase — not in code, not in examples, not in comments.

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
- **92 tests, all offline** — no network, no LLM, no MCP subprocess. `pytest -q`.

---

## Architecture

| file | what it does |
|------|--------------|
| `schema.py` | the strategy grammar as Pydantic models — the closed vocabulary the LLM must emit |
| `generator.py` | LLM strategy generation, validation-retry loop, dedup by canonical hash |
| `seeds.py` | hand-written seed candidates (run through the identical gate) |
| `data.py` | daily-bar fetch via `alpaca-py` + SQLite range cache (fetches only missing ranges) |
| `engine.py` | the hand-written, lookahead-free replay engine |
| `gate.py` | the promotion gate — three thresholds, a written reason for every verdict |
| `mcp_client.py` | the Alpaca MCP order path — stdio subprocess driven by `fastmcp` |
| `risk.py` | pre-trade checks — kill switch, max concurrent positions, max notional |
| `cycle.py` | `run_cycle()` — generate → backtest → gate → execute → persist, one function |
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

pytest -q                        # 92 tests, all offline

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
- **Forward tracking is a historical simulation.** The regret ledger runs "as of"
  a fixed past date (2026-06-17 on the committed seed) so genuinely unseen bars
  exist after it. This is **not** weeks of live shadow trading. The code, the
  API and the dashboard all label it as a simulation.
- **The selection-bias number has n = 12 vs n = 63.** On the committed seed the
  promoted-minus-rejected forward spread is +4.64pp (promoted +5.66%, n = 12;
  rejected +1.02%, n = 63), across 24 symbols. Still a small promoted sample, and
  the dispersion is wide both ways — the gate rejected the two biggest forward
  winners (both MSFT, +27–29%) and the median spread is only +2.7pp. The number
  exists to show the instrument works — not to claim the gate selects signal.
  "Promoted" still means only "passed the gate on its window".
- **Paper trading only.** No live trading path exists in the codebase.
- **It does not learn.** No training, no weights, no online updates
  ([DECISIONS.md](DECISIONS.md) D9). It revises decisions when forward evidence
  contradicts them — that is the entire claim, and it is not overstated
  elsewhere, so do not read more into it here.

---

## What would come next with more time

- More cycles and a genuine forward window — the honest fix for every "sample too
  small" line above is time and volume, not code.
- A scheduler (`T5.3`, deferred) running cycles unattended, with the dashboard
  still rendering from stored data alone.
- Widen the grammar (more indicators, position sizing) once there is enough
  forward data to justify the added surface.
- A paid deploy tier with a persistent disk, so cycles triggered from the
  dashboard survive a spin-down.
