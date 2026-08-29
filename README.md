# Self-auditing trading agent

An autonomous trading agent on Alpaca **paper** that audits its own decisions.
It generates candidate strategies with an LLM, backtests them on historical
daily bars with a hand-written replay engine, promotes only those that clear a
fixed gate, and routes promoted strategies to paper orders **through the Alpaca
MCP server**. Every promote/reject is stored with the exact numeric reason.

Built for the Alpaca AI Trading Agents Hackathon (lablab.ai).

**What it claims:** it revises decisions when the evidence contradicts them.
**What it does *not* claim:** that it learns, predicts better over time, or
generates alpha. See [DECISIONS.md](DECISIONS.md) D9.

---

## The loop

1. **Generate** — an LLM emits strategies as *data* in a closed grammar
   (fixed indicators and operators), validated by Pydantic. It never writes
   code (`generator.py`, `schema.py`).
2. **Backtest** — a plain, hand-written replay loop. Decisions are made on
   bar N's close; fills happen at bar N+1's open. No lookahead, no backtesting
   library (`engine.py`).
3. **Gate** — three thresholds in one dict (`min_total_return`,
   `max_drawdown`, `min_trades`). Every candidate gets a written reason
   (`gate.py`).
4. **Execute** — a promoted strategy produces a paper order via the Alpaca
   **MCP server**, driven over a stdio subprocess (`mcp_client.py`). Risk
   checks (kill switch, position and notional limits) run before every order
   with no path around them (`risk.py`).
5. **Persist** — one SQLite file holds strategies, runs, backtests, decisions,
   and orders (`db.py`).

Phase 4 (the "regret ledger" — shadow portfolios of rejected candidates and
automatic retirement of an active strategy a shadow beats) is scaffolded in the
schema but not yet built.

---

## Architecture

```
generator.py ─┐
              ├─► cycle.run_cycle() ──► db.py  (bars_cache.db / seed.db / volume)
seeds.py ─────┘        │  │  │
                       │  │  └─► mcp_client.py ──► alpaca-mcp-server (stdio subprocess) ──► Alpaca paper
                       │  └────► gate.py
data.py ──► engine.py ─┘         risk.py

api.py (FastAPI)
  GET  /                      dashboard (static/index.html, no build step)
  GET  /api/health
  GET  /api/runs, /api/runs/{id}
  GET  /api/strategies, /api/strategies/{id}
  GET  /api/orders
  GET  /api/equity-curves     latest curve per promoted strategy (the hero plot)
  POST /api/cycle             runs a cycle in the background, returns a run id
  GET  /api/mcp-check         explicit round trip through the MCP server
```

Every GET renders from stored rows only — no live market, scheduler, or network
call is needed to load the dashboard (D6). `POST /api/cycle` is the one write
path; it is rate-limited to one cycle per 60s in-process and never runs two at
once.

---

## Run it locally

Requires Python 3.13 and an Alpaca **paper** key + an OpenAI key.

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # then fill in the keys

# tests (all offline — no network, no LLM, no MCP subprocess)
pytest -q

# the API + dashboard
uvicorn api:app --reload
# open http://127.0.0.1:8000
```

### Environment variables

| var | required | purpose |
|-----|----------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | yes | paper account — data + MCP order path |
| `OPENAI_API_KEY` | yes | strategy generation (`gpt-4o-mini`) |
| `DB_PATH` | no | SQLite file location (default: `bars_cache.db`; on Railway: the volume, e.g. `/data/trading.db`) |
| `DRY_RUN` | no | truthy => no orders are submitted; the rest of the path still runs and logs |
| `KILL_SWITCH` | no | truthy => every order is blocked with a logged reason |
| `CYCLE_MIN_INTERVAL_S` | no | rate limit for `POST /api/cycle` (default 60) |

The MCP subprocess is always forced to `ALPACA_PAPER_TRADE=true` — no live
endpoint is constructed anywhere in the codebase.

### Rebuild the seed database

```bash
python scripts/seed.py            # step 1 live (real paper orders via MCP)
python scripts/seed.py --all-dry  # every step dry
```

This writes `seed.db` (committed). On a fresh deploy `api.py` copies it onto the
persistent volume on first boot, so the dashboard is populated immediately.

---

## Deploy (Railway)

1. `railway init` in this repo, add a **volume** mounted at `/data`.
2. Set the env vars above, with `DB_PATH=/data/trading.db`, `DRY_RUN` unset.
3. `railway up`. Nixpacks builds from `requirements.txt`; the start command is
   in `railway.json` / `Procfile`.
4. After the first deploy, hit `GET /api/mcp-check` once to confirm the Alpaca
   MCP subprocess runs in the Linux container.

---

## Limitations (read this)

- **Paper trading only.** No live endpoint is ever constructed. Orders you see
  are Alpaca paper orders.
- **IEX data, not SIP.** The free tier serves a small fraction of total volume.
  Daily bars only — nothing here is valid at intraday resolution (D5).
- **Small sample.** Backtests run on roughly one year of daily bars — a few
  hundred rows. A strategy that clears the gate on this window has cleared a
  low bar; three realised round-trips would be noise, which is why `min_trades`
  is 10 (D21).
- **Selection bias is not yet measured.** Phase 4 will compare the forward
  performance of promoted vs rejected candidates and report that number even if
  it is unflattering (D10). Until then, "promoted" means only "passed the gate
  on the backtest window", not "works".
- **No learning.** There is no model training, no weight update, no online
  learning of any kind. The agent selects and retires strategies on stored
  evidence — that is the whole claim (D9).
- **The LLM is not load-bearing.** It only ever emits grammar-constrained JSON
  that is validated before use; it never emits code (D3).
- **One cycle at a time.** `POST /api/cycle` is rate-limited and single-flighted
  so a visitor cannot spam the OpenAI key or the paper account.

---

## Repo map

| file | what |
|------|------|
| `schema.py` | the strategy grammar as Pydantic models |
| `data.py` | daily-bar fetch + SQLite cache (fetches only missing ranges) |
| `engine.py` | the hand-written, lookahead-free replay engine |
| `gate.py` | the promotion gate — thresholds + written reasons |
| `generator.py` | LLM strategy generation + validation retry + dedup |
| `seeds.py` | hand-written seed candidates (go through the identical gate) |
| `mcp_client.py` | the Alpaca MCP order path (stdio subprocess) |
| `risk.py` | pre-trade risk controls — kill switch, position + notional limits |
| `db.py` | SQLite persistence (one file for the whole system) |
| `cycle.py` | `run_cycle()` — generate → backtest → gate → execute → persist |
| `api.py` | FastAPI read endpoints + `POST /api/cycle` + the dashboard |
| `static/index.html` | the dashboard — one file, no build step |
| `scripts/seed.py` | builds `seed.db` |
| `DECISIONS.md` | every choice a reviewer might question |
| `ROADMAP.md` | phased build plan with acceptance criteria |
