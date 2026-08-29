"""The regret ledger (Phase 4) — forward-tracking shadows and the selection-bias check.

This is the differentiator, and the claim it supports is narrow (D9): the agent
**revises a decision when forward evidence contradicts it**. It does not learn,
predict better over time, or generate alpha. Nothing here may be described as
more than that.

What it does, for a single as-of date ``T0`` in the past:

  * For every stored strategy, split its bars at ``T0``:
      - **in-sample** ``[T0 - lookback, T0]`` — re-run the promotion gate on this
        alone, giving the decision that *would* have been made at ``T0`` knowing
        only data up to ``T0``;
      - **forward** ``(T0, today]`` — replay the same strategy forward on bars it
        never saw at decision time (``engine.run_backtest(start_index=...)`` — the
        pre-``T0`` bars are indicator warm-up only, D34). This is the shadow
        portfolio's equity curve, measured from the decision point.
    Both halves are stored as ``backtests`` rows (``kind`` = 'insample' /
    'forward', ``as_of`` = ``T0``) — history is preserved, never overwritten.

  * **Retirement** (``retire.py``): a rejected shadow that beat an as-of-promoted
    active strategy forward, by the policy margin, over a long-enough window,
    with the active also losing forward — retires it. Active -> 'retired',
    shadow -> 'active', a ``decisions`` row records it, any open paper position
    of the retired strategy is closed through the MCP path, and a post-mortem
    (``postmortem.py``) is stored.

  * **Selection-bias check** (T4.5): mean forward return of as-of-promoted vs
    as-of-rejected strategies, with sample sizes. Reported as-is (D10).

**This is a historical simulation of forward tracking, not weeks of live
results.** ``T0`` is in the past so that genuinely unseen bars exist after it;
the code and the dashboard say so explicitly and never blur the two.
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
import retire
from retire import ShadowRecord
from schema import Strategy

log = logging.getLogger("regret")

BARS_LOOKBACK_DAYS = 400          # ~250 trading days of in-sample history before T0
FORWARD_BARS_TARGET = 50          # T0 is set to leave this many forward trading bars
MIN_INSAMPLE_BARS = 60           # below this the in-sample gate decision is not meaningful

_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day",
    "dry_run", "blocked", "error", "no_position",
}


@dataclass
class SelectionBias:
    promoted_avg_forward_return_pct: float | None
    rejected_avg_forward_return_pct: float | None
    n_promoted: int
    n_rejected: int
    as_of: str

    @property
    def spread_pp(self) -> float | None:
        if self.promoted_avg_forward_return_pct is None or self.rejected_avg_forward_return_pct is None:
            return None
        return self.promoted_avg_forward_return_pct - self.rejected_avg_forward_return_pct

    def headline(self) -> str:
        if self.spread_pp is None:
            return f"selection-bias check: not enough forward data as of {self.as_of}"
        return (
            f"as of {self.as_of}: promoted candidates averaged "
            f"{self.promoted_avg_forward_return_pct:+.2f}% forward "
            f"(n={self.n_promoted}), rejected averaged "
            f"{self.rejected_avg_forward_return_pct:+.2f}% (n={self.n_rejected}) — "
            f"spread {self.spread_pp:+.2f}pp"
        )


@dataclass
class RegretResult:
    run_id: int
    as_of: str
    records: list[ShadowRecord] = field(default_factory=list)
    retirements: list[dict] = field(default_factory=list)
    selection_bias: SelectionBias | None = None
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"regret ledger run {self.run_id}, as of {self.as_of}:"]
        lines.append(f"  evaluated {len(self.records)} strateg(ies), "
                     f"skipped {len(self.skipped)}")
        for r in self.records:
            lines.append(
                f"  [{r.as_of_decision:8}] {r.name} ({r.symbol}) — "
                f"in-sample {r.insample_return_pct:+.2f}%, "
                f"forward {r.forward_return_pct:+.2f}% over {r.forward_bars} bars"
            )
        if self.retirements:
            for rt in self.retirements:
                lines.append(f"  RETIRED: {rt['reason']}")
        else:
            lines.append("  no retirements fired — no shadow met the policy")
        if self.selection_bias:
            lines.append("  " + self.selection_bias.headline())
        return "\n".join(lines)


def _split_index(bars, as_of: date) -> int:
    """First index whose bar date is strictly after ``as_of``."""
    for i, b in enumerate(bars):
        if b["timestamp"].date() > as_of:
            return i
    return len(bars)


def _pick_as_of(bars_by_symbol: dict[str, list], forward_bars_target: int) -> date:
    """Choose ``T0`` to leave ~``forward_bars_target`` forward trading bars.

    Principled, not outcome-searched (D35): T0 is the date of the bar that sits
    ``forward_bars_target`` positions from the end of the longest available
    series. US equities share a trading calendar, so this leaves the same count
    for every symbol give or take a holiday. ``forward_bars_target`` is 25% above
    the retirement policy's ``min_forward_bars`` so a retirement *can* fire on a
    long-enough window — whether one does is left to the data.
    """
    longest = max(bars_by_symbol.values(), key=len)
    idx = max(0, len(longest) - 1 - forward_bars_target)
    return longest[idx]["timestamp"].date()


def _metrics_only(m: dict) -> dict:
    return {k: v for k, v in m.items() if k != "equity_curve"}


def _load_strategies(conn, strategy_ids: list[int] | None) -> list[tuple[int, Strategy]]:
    if strategy_ids is None:
        rows = conn.execute("SELECT id, schema_json FROM strategies ORDER BY id").fetchall()
    else:
        qs = ",".join("?" * len(strategy_ids))
        rows = conn.execute(
            f"SELECT id, schema_json FROM strategies WHERE id IN ({qs}) ORDER BY id",
            tuple(strategy_ids),
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append((int(r["id"]), Strategy.model_validate_json(r["schema_json"])))
        except Exception as exc:  # noqa: BLE001
            log.warning("strategy %s has an unparseable schema, skipping: %s", r["id"], exc)
    return out


def _open_position_symbol(conn, strategy_id: int) -> bool:
    for row in db.list_orders(conn):
        if row["strategy_id"] == strategy_id and row["side"] == "buy" \
                and str(row["status"]).lower() not in _TERMINAL_ORDER_STATUSES:
            return True
    return False


def selection_bias(records: list[ShadowRecord], as_of: str) -> SelectionBias:
    prom = [r.forward_return_pct for r in records if r.as_of_decision == "promoted"]
    rej = [r.forward_return_pct for r in records if r.as_of_decision == "rejected"]
    return SelectionBias(
        promoted_avg_forward_return_pct=round(sum(prom) / len(prom), 2) if prom else None,
        rejected_avg_forward_return_pct=round(sum(rej) / len(rej), 2) if rej else None,
        n_promoted=len(prom),
        n_rejected=len(rej),
        as_of=as_of,
    )


def run_regret_ledger(
    *,
    as_of: date | str | None = None,
    conn=None,
    db_path=None,
    dry_run: bool | None = None,
    strategy_ids: list[int] | None = None,
    forward_bars_target: int = FORWARD_BARS_TARGET,
    lookback_days: int = BARS_LOOKBACK_DAYS,
    close_fn=None,
    postmortem_fn=None,
) -> RegretResult:
    """Run the regret ledger for one as-of date. See the module docstring.

    ``as_of`` — a past date; ``None`` picks it to leave ``forward_bars_target``
    forward bars (D35). ``close_fn(symbol, dry_run=)`` and ``postmortem_fn(facts)``
    are injectable for tests; they default to ``mcp_client.close_position`` and
    ``postmortem.generate_postmortem``.
    """
    own_conn = conn is None
    conn = conn or db.connect(db_path or db.DB_PATH)

    if close_fn is None:
        import mcp_client
        close_fn = mcp_client.close_position
    if postmortem_fn is None:
        import postmortem
        postmortem_fn = postmortem.generate_postmortem

    try:
        strategies = _load_strategies(conn, strategy_ids)
        if not strategies:
            raise ValueError("no strategies to evaluate")

        today = date.today()
        bars_by_symbol: dict[str, list] = {}
        for _sid, strat in strategies:
            sym = strat.symbol.upper()
            if sym not in bars_by_symbol:
                bars_by_symbol[sym] = data.get_bars(
                    sym, today - timedelta(days=lookback_days + 400), today
                )

        if isinstance(as_of, str):
            as_of = date.fromisoformat(as_of)
        if as_of is None:
            as_of = _pick_as_of(bars_by_symbol, forward_bars_target)
        as_of_s = as_of.isoformat()
        log.info("=== regret ledger: as of %s (today %s) ===", as_of_s, today)

        run_id = db.start_run(conn)
        db.set_run_as_of(conn, run_id, as_of_s)

        records: list[ShadowRecord] = []
        skipped: list[tuple[str, str]] = []

        for sid, strat in strategies:
            sym = strat.symbol.upper()
            all_bars = [b for b in bars_by_symbol[sym]
                        if b["timestamp"].date() >= as_of - timedelta(days=lookback_days)]
            split = _split_index(all_bars, as_of)
            insample = all_bars[:split]
            forward_bars = len(all_bars) - split

            if len(insample) < MIN_INSAMPLE_BARS:
                skipped.append((strat.name, f"only {len(insample)} in-sample bars"))
                continue
            if forward_bars < 2 or split >= len(all_bars) - 1:
                skipped.append((strat.name, f"only {forward_bars} forward bars"))
                continue

            insample_m = engine.run_backtest(strat, insample)
            forward_m = engine.run_backtest(strat, all_bars, start_index=split)

            db.insert_backtest(
                conn, strategy_id=sid, run_id=run_id,
                metrics_json=json.dumps(_metrics_only(insample_m)),
                equity_curve_json=json.dumps(insample_m["equity_curve"], default=str),
                bars_start=str(insample[0]["timestamp"].date()),
                bars_end=str(insample[-1]["timestamp"].date()),
                kind="insample", as_of=as_of_s,
            )
            db.insert_backtest(
                conn, strategy_id=sid, run_id=run_id,
                metrics_json=json.dumps({**_metrics_only(forward_m),
                                         "forward_bars": forward_bars}),
                equity_curve_json=json.dumps(forward_m["equity_curve"], default=str),
                bars_start=str(all_bars[split]["timestamp"].date()),
                bars_end=str(all_bars[-1]["timestamp"].date()),
                kind="forward", as_of=as_of_s,
            )

            gr = gate.evaluate(insample_m)
            decision = "promoted" if gr.promoted else "rejected"
            db.insert_decision(
                conn, strategy_id=sid, run_id=run_id, outcome=decision,
                reason=f"as-of {as_of_s} gate: {gr.reason}",
            )
            records.append(ShadowRecord(
                strategy_id=sid, name=strat.name, symbol=sym,
                as_of_decision=decision, insample_metrics=_metrics_only(insample_m),
                forward_metrics={**_metrics_only(forward_m), "forward_bars": forward_bars},
                forward_bars=forward_bars,
            ))

        # --- retirement pass ---
        retirements_out: list[dict] = []
        for rt in retire.find_retirements(records):
            log.info("RETIREMENT: %s", rt.reason)
            db.set_strategy_status(conn, rt.retired.strategy_id, "retired")
            db.set_strategy_status(conn, rt.winner.strategy_id, "active")
            decision_id = db.insert_decision(
                conn, strategy_id=rt.retired.strategy_id, run_id=run_id,
                outcome="retired", reason=rt.reason,
            )

            close_status = None
            if _open_position_symbol(conn, rt.retired.strategy_id):
                res = close_fn(rt.retired.symbol, dry_run=dry_run)
                close_status = res.status
                db.insert_order(
                    conn, strategy_id=rt.retired.strategy_id, run_id=run_id,
                    symbol=rt.retired.symbol, qty=res.qty, side="sell",
                    status=res.status, broker_order_id=res.broker_order_id,
                    submitted_via="mcp", dry_run=(res.status == "dry_run"),
                    raw_response=res.raw or (res.error or ""),
                )
                log.info("closed %s position for retired strategy: %s",
                         rt.retired.symbol, res.status)

            text = postmortem_fn(rt.facts)
            db.insert_postmortem(
                conn, decision_id=decision_id, run_id=run_id,
                retired_strategy_id=rt.retired.strategy_id,
                promoted_strategy_id=rt.winner.strategy_id,
                facts_json=json.dumps(rt.facts, default=str), text=text,
            )
            retirements_out.append({
                "reason": rt.reason,
                "retired": rt.retired.name,
                "promoted": rt.winner.name,
                "symbol": rt.retired.symbol,
                "close_status": close_status,
                "postmortem": text,
            })

        sb = selection_bias(records, as_of_s)
        n_prom = sum(1 for r in records if r.as_of_decision == "promoted")
        db.finish_run(conn, run_id, n_generated=len(records),
                      n_promoted=n_prom, n_rejected=len(records) - n_prom)
        log.info("=== regret ledger done (run %d) ===", run_id)

        return RegretResult(
            run_id=run_id, as_of=as_of_s, records=records,
            retirements=retirements_out, selection_bias=sb, skipped=skipped,
        )
    finally:
        if own_conn:
            conn.close()


def main() -> None:  # manual: `python regret.py`
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_regret_ledger()
    print("\n" + result.summary())


if __name__ == "__main__":
    main()
