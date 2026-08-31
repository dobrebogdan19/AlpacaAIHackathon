"""Position management — the exit path for open option positions (T5.3).

An agent that only opens positions is not managing anything. Options expire; a
long call left alone either decays to nothing or has to be babysat through
expiry. So every scheduler tick, while the market is open, runs
``run_management_sweep``: it reads the real open positions from the Alpaca MCP
server, applies the exit rules below to each, and closes the ones that hit a
rule — through the same MCP path the entries use.

The rules live in one dict (cf. ``gate.py`` / ``options.SELECTION_RULES``):

  * ``profit_target_pct`` — take the gain. A long call is convex but decays;
    round-tripping a solid profit back to zero as theta bites is the common way
    these lose. Close once the position is up this much on the premium paid.
  * ``stop_loss_pct`` — cap the bleed. Max loss on a long call is 100% of
    premium; half of that is a natural line that still leaves room for ordinary
    noise.
  * ``max_dte_to_hold`` — time out. Inside this many calendar days to expiry,
    gamma/theta dominate and the underlying-signal thesis no longer has time to
    play out. Close and let a fresh cycle re-enter with full DTE if the signal
    still holds.

Every position looked at is logged, whether or not it is closed — the record
shows the agent managing, not just the closes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import db
import mcp_client
import options
import risk

log = logging.getLogger("mgmt")

# --- exit rules — the only knobs, one place -------------------------------
EXIT_RULES: dict[str, float] = {
    "profit_target_pct": 60.0,   # close when up >= +60% on the premium paid
    "stop_loss_pct": 50.0,       # close when down >= -50% on the premium paid
    "max_dte_to_hold": 7,        # close any contract with <= 7 calendar days to expiry
}

_OPTION_ASSET_CLASSES = {"us_option", "option", "option_us"}


@dataclass
class PositionView:
    occ_symbol: str
    underlying: str
    qty: float
    pnl_pct: float
    dte: int
    avg_entry_price: float
    current_price: float


@dataclass
class SweepAction:
    occ_symbol: str
    pnl_pct: float
    dte: int
    action: str          # 'closed' | 'held' | 'close-failed' | 'error'
    reason: str


@dataclass
class SweepResult:
    evaluated: int = 0
    closed: int = 0
    held: int = 0
    failed: int = 0
    dry_run: bool = False
    error: str | None = None
    actions: list[SweepAction] = field(default_factory=list)

    def summary(self) -> str:
        if self.error:
            return f"sweep error: {self.error}"
        return (f"{self.evaluated} option position(s): {self.closed} closed, "
                f"{self.held} held, {self.failed} close-failed"
                f"{' [dry-run]' if self.dry_run else ''}")


def _is_option_position(pos: dict) -> bool:
    ac = str(pos.get("asset_class", "")).lower()
    if ac in _OPTION_ASSET_CLASSES:
        return True
    sym = str(pos.get("symbol", ""))
    try:
        options.parse_occ(sym)
        return True
    except ValueError:
        return False


def _view(pos: dict, *, today: date | None = None) -> PositionView | None:
    today = today or date.today()
    occ = str(pos.get("symbol", ""))
    try:
        root, expiry, _right, _strike = options.parse_occ(occ)
    except ValueError:
        return None

    def _f(*keys, default=0.0):
        for k in keys:
            v = pos.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return default

    qty = _f("qty", "qty_available", default=0.0)
    avg = _f("avg_entry_price", "cost_basis", default=0.0)
    cur = _f("current_price", "asset_current_price", default=0.0)

    plpc = pos.get("unrealized_plpc")
    if plpc not in (None, ""):
        pnl_pct = float(plpc) * 100.0
    elif avg > 0 and cur > 0:
        pnl_pct = (cur - avg) / avg * 100.0
    else:
        pnl_pct = 0.0

    return PositionView(
        occ_symbol=occ, underlying=root, qty=qty, pnl_pct=round(pnl_pct, 2),
        dte=(expiry - today).days, avg_entry_price=avg, current_price=cur,
    )


def evaluate_exit(pnl_pct: float, dte: int, rules: dict | None = None) -> tuple[bool, str]:
    """Decide whether an open long call should be closed now. Pure logic."""
    r = {**EXIT_RULES, **(rules or {})}
    if dte <= r["max_dte_to_hold"]:
        return True, (f"{dte} DTE <= {int(r['max_dte_to_hold'])} — closing before "
                      f"expiry/gamma risk (P&L {pnl_pct:+.1f}%)")
    if pnl_pct >= r["profit_target_pct"]:
        return True, (f"P&L {pnl_pct:+.1f}% >= +{r['profit_target_pct']:.0f}% profit "
                      f"target ({dte} DTE)")
    if pnl_pct <= -abs(r["stop_loss_pct"]):
        return True, (f"P&L {pnl_pct:+.1f}% <= -{abs(r['stop_loss_pct']):.0f}% stop "
                      f"({dte} DTE)")
    return False, (f"hold: P&L {pnl_pct:+.1f}% within "
                   f"(-{abs(r['stop_loss_pct']):.0f}%, +{r['profit_target_pct']:.0f}%), "
                   f"{dte} DTE > {int(r['max_dte_to_hold'])}")


def _matching_buy(conn, occ: str):
    """The most recent BUY order row for this contract, for strategy/run linkage."""
    for row in reversed(db.list_orders(conn)):
        if row["contract_symbol"] == occ and str(row["side"]).lower() == "buy":
            return row
    return None


def run_management_sweep(conn, *, dry_run: bool | None = None,
                         rules: dict | None = None) -> SweepResult:
    """Read open positions from the broker, apply ``EXIT_RULES``, close matches.

    Never raises — a broker read failure comes back as ``SweepResult(error=...)``.
    """
    is_dry = risk.dry_run_active(dry_run)
    res = SweepResult(dry_run=is_dry)

    try:
        positions = mcp_client.list_positions()
    except Exception as exc:  # noqa: BLE001
        log.warning("sweep: could not read positions (%s)", exc)
        res.error = str(exc)
        return res

    opts = [p for p in positions if _is_option_position(p)]
    log.info("sweep: %d position(s) total, %d option(s)", len(positions), len(opts))

    for pos in opts:
        view = _view(pos)
        if view is None or view.qty <= 0:
            continue
        res.evaluated += 1
        should_close, reason = evaluate_exit(view.pnl_pct, view.dte, rules)

        if not should_close:
            res.held += 1
            res.actions.append(SweepAction(view.occ_symbol, view.pnl_pct, view.dte,
                                           "held", reason))
            log.info("sweep: HOLD %s — %s", view.occ_symbol, reason)
            continue

        log.info("sweep: CLOSE %s — %s", view.occ_symbol, reason)
        buy = _matching_buy(conn, view.occ_symbol)

        if is_dry:
            res.closed += 1
            res.actions.append(SweepAction(view.occ_symbol, view.pnl_pct, view.dte,
                                           "closed", reason + " [dry-run]"))
            if buy is not None:
                db.insert_order(
                    conn, strategy_id=buy["strategy_id"], run_id=buy["run_id"],
                    symbol=view.occ_symbol, qty=view.qty, side="sell", status="dry_run",
                    submitted_via="mcp", dry_run=True, raw_response="dry_run: exit rule hit",
                    asset_class="option", contract_symbol=view.occ_symbol,
                    underlying=view.underlying, selection_reason=f"EXIT — {reason}",
                )
            continue

        close = mcp_client.close_position(view.occ_symbol)
        ok = close.ok
        if buy is not None:
            db.insert_order(
                conn, strategy_id=buy["strategy_id"], run_id=buy["run_id"],
                symbol=view.occ_symbol, qty=view.qty, side="sell",
                status=close.status, broker_order_id=close.broker_order_id,
                submitted_via="mcp", dry_run=False,
                raw_response=close.raw or (close.error or ""),
                asset_class="option", contract_symbol=view.occ_symbol,
                underlying=view.underlying, selection_reason=f"EXIT — {reason}",
            )
            if ok:
                # stop the risk caps counting a position we have just closed out.
                conn.execute("UPDATE orders SET status = 'closed' WHERE id = ?",
                             (buy["id"],))
                conn.commit()

        if ok:
            res.closed += 1
            res.actions.append(SweepAction(view.occ_symbol, view.pnl_pct, view.dte,
                                           "closed", reason))
        else:
            res.failed += 1
            res.actions.append(SweepAction(view.occ_symbol, view.pnl_pct, view.dte,
                                           "close-failed", f"{reason} — {close.error}"))
            log.error("sweep: close FAILED for %s — %s", view.occ_symbol, close.error)

    return res
