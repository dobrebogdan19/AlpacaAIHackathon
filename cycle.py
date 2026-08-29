"""One full agent cycle, end to end: generate -> backtest -> gate -> execute -> persist.

``run_cycle(...)`` is a plain function, callable programmatically (Phase 5 will
expose it over HTTP). It never reads from stdin and never needs an open market —
a decision on the last bar is dropped by the engine, and orders queue when the
market is closed.

Flow per candidate:
  1. fetch daily bars (SDK, cached — ``data.py``)
  2. replay (``engine.run_backtest`` — lookahead-free, hand-written)
  3. gate (``gate.evaluate``) — a ``decisions`` row is written for EVERY
     candidate, promoted or rejected, with the gate's written reason
  4. promoted only: risk checks (``risk.check``), then an order through the
     Alpaca MCP server (``mcp_client``). The order (or the block, or the dry
     run) is persisted to ``orders`` with the strategy that caused it.

Nothing is silently dropped: every candidate ends with a decision row, and every
promotion ends with an order row (status ``dry_run`` / ``blocked`` / an Alpaca
status / ``error``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import data
import db
import engine
import gate
import mcp_client
import risk

log = logging.getLogger("cycle")

# Fixed notional per position (CLAUDE.md). One knob, here.
FIXED_ORDER_NOTIONAL_USD = 1_000.0
BARS_LOOKBACK_DAYS = 400          # ~250 trading days


@dataclass
class StrategyOutcome:
    strategy_id: int
    name: str
    symbol: str
    metrics: dict | None
    promoted: bool
    decision_reason: str
    order_status: str | None = None
    broker_order_id: str | None = None
    order_blocked_reason: str | None = None


@dataclass
class CycleResult:
    run_id: int
    n_generated: int
    n_promoted: int
    n_rejected: int
    n_orders_submitted: int
    n_orders_blocked: int
    dry_run: bool
    outcomes: list[StrategyOutcome] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"run {self.run_id}: generated {self.n_generated}, "
            f"promoted {self.n_promoted}, rejected {self.n_rejected}, "
            f"orders {self.n_orders_submitted} (blocked {self.n_orders_blocked}), "
            f"dry_run={self.dry_run}",
        ]
        for o in self.outcomes:
            tag = "PROMOTED" if o.promoted else "REJECTED"
            lines.append(f"  [{tag}] {o.name} ({o.symbol}) — {o.decision_reason}")
            if o.order_blocked_reason:
                lines.append(f"           order BLOCKED: {o.order_blocked_reason}")
            elif o.order_status:
                lines.append(f"           order: {o.order_status} "
                             f"(id={o.broker_order_id or '-'})")
        return "\n".join(lines)


def run_cycle(
    *,
    symbols: list[str] | None = None,
    n: int = 5,
    dry_run: bool | None = None,
    conn=None,
    db_path=None,
    generate_fn=None,
    order_notional: float = FIXED_ORDER_NOTIONAL_USD,
    lookback_days: int = BARS_LOOKBACK_DAYS,
) -> CycleResult:
    """Run one cycle. Returns a :class:`CycleResult`.

    ``conn`` — reuse an open DB connection (tests do); otherwise one is opened on
    ``db_path`` (default: the project DB) and closed at the end.
    ``generate_fn`` — override strategy generation (tests inject a fake). Must
    accept ``n``, ``symbols``, ``conn``, ``run_id`` and return an object with
    ``.strategies`` (list[Strategy]) and ``.strategy_ids`` (list[int]).
    ``dry_run`` — forwarded to ``risk`` / ``mcp_client``; ``None`` => the
    ``DRY_RUN`` env var decides.
    """
    own_conn = conn is None
    conn = conn or db.connect(db_path or db.DB_PATH)
    is_dry = risk.dry_run_active(dry_run)

    if generate_fn is None:
        import generator
        generate_fn = generator.generate

    try:
        run_id = db.start_run(conn)
        log.info("=== cycle start (run %d, dry_run=%s) ===", run_id, is_dry)

        gen = generate_fn(n=n, symbols=symbols, conn=conn, run_id=run_id)
        strategies = list(gen.strategies)
        strategy_ids = list(gen.strategy_ids)
        log.info("generated %d candidate strategy(ies)", len(strategies))

        end = date.today()
        start = end - timedelta(days=lookback_days)

        outcomes: list[StrategyOutcome] = []
        n_promoted = n_rejected = n_orders = n_blocked = 0

        for strat, sid in zip(strategies, strategy_ids):
            bars = data.get_bars(strat.symbol, start, end)

            if len(bars) < 2:
                reason = f"insufficient history: only {len(bars)} bar(s) for {strat.symbol}"
                db.insert_decision(conn, strategy_id=sid, run_id=run_id,
                                   outcome="rejected", reason=reason)
                db.set_strategy_status(conn, sid, "rejected")
                n_rejected += 1
                outcomes.append(StrategyOutcome(sid, strat.name, strat.symbol, None,
                                                False, reason))
                log.info("REJECTED %s — %s", strat.name, reason)
                continue

            metrics = engine.run_backtest(strat, bars)
            db.insert_backtest(
                conn, strategy_id=sid, run_id=run_id,
                metrics_json=json.dumps({k: v for k, v in metrics.items()
                                         if k != "equity_curve"}),
                equity_curve_json=json.dumps(metrics["equity_curve"], default=str),
                bars_start=str(bars[0]["timestamp"].date()),
                bars_end=str(bars[-1]["timestamp"].date()),
            )

            gr = gate.evaluate(metrics)
            db.insert_decision(
                conn, strategy_id=sid, run_id=run_id,
                outcome="promoted" if gr.promoted else "rejected", reason=gr.reason,
            )
            db.set_strategy_status(conn, sid, "active" if gr.promoted else "rejected")

            outcome = StrategyOutcome(
                strategy_id=sid, name=strat.name, symbol=strat.symbol,
                metrics={k: v for k, v in metrics.items() if k != "equity_curve"},
                promoted=gr.promoted, decision_reason=gr.reason,
            )

            if not gr.promoted:
                n_rejected += 1
                log.info("REJECTED %s — %s", strat.name, gr.reason)
                outcomes.append(outcome)
                continue

            n_promoted += 1
            log.info("PROMOTED %s — %s", strat.name, gr.reason)

            # ---- risk checks before ANY order (T3.3) ----
            rd = risk.check(conn, notional=order_notional, dry_run=dry_run)
            if not rd.allowed:
                db.insert_order(
                    conn, strategy_id=sid, run_id=run_id, symbol=strat.symbol,
                    qty=None, side="buy", status="blocked", broker_order_id=None,
                    submitted_via="mcp", dry_run=is_dry, raw_response=rd.reason,
                )
                n_blocked += 1
                outcome.order_status = "blocked"
                outcome.order_blocked_reason = rd.reason
                outcomes.append(outcome)
                continue

            # ---- order via the Alpaca MCP server (T3.2 / D7) ----
            res = mcp_client.submit_market_order(
                strat.symbol, notional=order_notional, side="buy", dry_run=dry_run,
            )
            db.insert_order(
                conn, strategy_id=sid, run_id=run_id, symbol=strat.symbol,
                qty=res.qty, side="buy", status=res.status,
                broker_order_id=res.broker_order_id, submitted_via="mcp",
                dry_run=is_dry, raw_response=res.raw or (res.error or ""),
            )
            if res.ok and res.status != "dry_run":
                n_orders += 1
            outcome.order_status = res.status
            outcome.broker_order_id = res.broker_order_id
            outcomes.append(outcome)

        db.finish_run(conn, run_id, n_generated=len(strategies),
                      n_promoted=n_promoted, n_rejected=n_rejected)
        log.info("=== cycle done (run %d) ===", run_id)

        return CycleResult(
            run_id=run_id, n_generated=len(strategies), n_promoted=n_promoted,
            n_rejected=n_rejected, n_orders_submitted=n_orders,
            n_orders_blocked=n_blocked, dry_run=is_dry, outcomes=outcomes,
        )
    finally:
        if own_conn:
            conn.close()


def main() -> None:  # manual demo: `python cycle.py`
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_cycle()
    print("\n" + result.summary())


if __name__ == "__main__":
    main()
