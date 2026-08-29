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
