"""Startup reconciliation — make the local ``orders`` table agree with the broker.

``risk.py`` enforces ``MAX_CONCURRENT_POSITIONS`` and the option-premium ceiling
by counting rows in ``orders`` that look "open". After a process restart (a Render
free-tier cold start re-seeds the DB from ``/tmp``, or a paid instance simply
resumes) some of those rows may no longer correspond to anything on the paper
account — a position that was closed while we were down, a working order that
filled or was cancelled, or seed data from a different account. Left alone they
would wrongly saturate the caps and block every new trade.

``reconcile_orders`` asks the Alpaca MCP server what is *actually* open
(``get_all_positions`` + ``get_orders`` status=open) and marks any still-"open"
local row whose symbol is not in that set as ``reconciled-closed`` (a terminal
status, see ``risk._TERMINAL_ORDER_STATUSES``).

It never opens or closes anything at the broker — it only corrects local
bookkeeping, and logs every change. If the MCP read fails it changes nothing and
says so: a stuck cap is safer than trading on a wrong picture of what we hold.

``backfill_positions`` is the other half (D58): after a restart that wiped the
DB, the broker still holds positions that now have no local ``orders`` row at
all. It inserts one ``reconstructed`` row per orphan position, built only from
broker facts (contract, quantity, cost basis) and pointing at a single shared
synthetic strategy. The strategy rules and the gate reason that opened the
position are gone; they are **not** invented. This is what lets ``risk.py`` and
the dashboard see the real exposure after a wipe.
"""

from __future__ import annotations

import logging

import db
import mcp_client
import options

log = logging.getLogger("reconcile")

# One shared placeholder strategy that every reconstructed order row points at.
# ``insert_strategy`` dedups on ``dedup_key``, so this is created once and reused.
_RECON_STRATEGY_NAME = "(positions reconstructed from the broker after a restart)"
_RECON_STRATEGY_DEDUP = "__reconstructed__"
_RECON_ORDER_STATUS = "broker-reconstructed"


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

# Local order statuses that claim an open position/working order. Anything here
# that the broker does not confirm gets flipped to ``reconciled-closed``.
_OPEN_LOCAL_STATUSES = {
    "accepted", "new", "pending_new", "partially_filled", "filled",
    "accepted_for_bidding", "held", "pending_replace", "replaced",
    _RECON_ORDER_STATUS,  # a reconstructed row the broker no longer confirms is closed too
}


def _symbol_of(row) -> str:
    keys = row.keys()
    if "contract_symbol" in keys and row["contract_symbol"]:
        return str(row["contract_symbol"])
    return str(row["symbol"])


def reconcile_orders(conn, *, dry_run: bool = False) -> dict:
    """Flip local order rows the broker cannot confirm to ``reconciled-closed``.

    Returns a summary dict: ``{"checked", "closed", "kept", "broker_symbols",
    "changes": [...], "skipped": reason|None}``.
    """
    local = [r for r in db.list_orders(conn)
             if str(r["status"]).lower() in _OPEN_LOCAL_STATUSES]
    if not local:
        log.info("reconcile: no open local order rows — nothing to do")
        return {"checked": 0, "closed": 0, "kept": 0, "broker_symbols": [],
                "changes": [], "skipped": None}

    try:
        positions = mcp_client.list_positions()
        open_orders = mcp_client.list_open_orders()
    except Exception as exc:  # noqa: BLE001 — a read failure must not change state
        log.warning("reconcile: broker read failed (%s) — leaving %d local row(s) as-is",
                    exc, len(local))
        return {"checked": len(local), "closed": 0, "kept": len(local),
                "broker_symbols": [], "changes": [], "skipped": f"broker read failed: {exc}"}

    live: set[str] = set()
    for p in positions:
        s = p.get("symbol")
        if s:
            live.add(str(s))
    for o in open_orders:
        s = o.get("symbol")
        if s:
            live.add(str(s))

    changes: list[dict] = []
    closed = 0
    for row in local:
        sym = _symbol_of(row)
        if sym in live:
            continue
        changes.append({"order_id": int(row["id"]), "symbol": sym,
                        "was": str(row["status"])})
        if not dry_run:
            conn.execute(
                "UPDATE orders SET status = 'reconciled-closed', "
                "raw_response = COALESCE(raw_response, '') || ' | reconciled: not open at broker' "
                "WHERE id = ?",
                (int(row["id"]),),
            )
        closed += 1
        log.info("reconcile: order %d (%s, was %s) not open at broker -> reconciled-closed%s",
                 row["id"], sym, row["status"], " [dry-run]" if dry_run else "")
    if not dry_run:
        conn.commit()

    kept = len(local) - closed
    log.info("reconcile: %d open local row(s) — %d confirmed, %d marked closed",
             len(local), kept, closed)
    return {"checked": len(local), "closed": closed, "kept": kept,
            "broker_symbols": sorted(live), "changes": changes, "skipped": None}


def _recon_strategy_id(conn) -> int:
    """Id of the shared synthetic strategy for reconstructed positions (created once)."""
    return db.insert_strategy(
        conn, name=_RECON_STRATEGY_NAME, symbol="—", schema_json="{}",
        rationale=None, source="manual", status="active",
        dedup_key=_RECON_STRATEGY_DEDUP,
    )


def _known_symbols(conn) -> set[str]:
    return {_symbol_of(r) for r in db.list_orders(conn)}


def backfill_positions(conn, *, dry_run: bool = False) -> dict:
    """Insert a reconstructed ``orders`` row for every open broker position that
    has no local row at all (D58).

    Built only from broker facts — contract, quantity, cost basis. The strategy
    and gate reason are lost and are not reconstructed; ``selection_reason`` says
    so plainly. Returns ``{"reconstructed", "symbols", "skipped"}``.

    Idempotent: a position that already has a local row (including one made by an
    earlier call) is left alone.
    """
    try:
        positions = mcp_client.list_positions()
    except Exception as exc:  # noqa: BLE001 — a read failure must not change state
        log.warning("backfill: broker read failed (%s) — nothing reconstructed", exc)
        return {"reconstructed": 0, "symbols": [], "skipped": f"broker read failed: {exc}"}

    known = _known_symbols(conn)
    made: list[str] = []
    for p in positions:
        sym = str(p.get("symbol") or "")
        if not sym or sym in known or _num(p.get("qty")) == 0:
            continue

        try:
            root, expiry, right, strike = options.parse_occ(sym)
            is_option = True
        except ValueError:
            root, expiry, right, strike = sym, None, None, None
            is_option = False

        qty = _num(p.get("qty"))
        cost_basis = _num(p.get("cost_basis"))
        if cost_basis <= 0:
            cost_basis = abs(_num(p.get("avg_entry_price"))) * abs(qty) * (100.0 if is_option else 1.0)

        reason = (
            f"Reconstructed from the broker on {db._now()[:10]}: {qty:g} "
            f"{'contract(s)' if is_option else 'share(s)'}, ~${cost_basis:,.2f} cost basis. "
            f"The strategy rules and the gate reason that opened this position were lost "
            f"when the database was wiped on a restart and are not reconstructed."
        )
        if not dry_run:
            sid = _recon_strategy_id(conn)
            db.insert_order(
                conn, strategy_id=sid, run_id=None, symbol=sym, qty=qty, side="buy",
                status=_RECON_ORDER_STATUS, broker_order_id=None, submitted_via="mcp",
                dry_run=False, raw_response="reconstructed from broker position (D58)",
                asset_class="option" if is_option else "equity",
                contract_symbol=sym if is_option else None, underlying=root,
                strike=strike, expiry=str(expiry) if expiry else None,
                premium=round(cost_basis, 2) if is_option else None,
                selection_reason=reason, reconstructed=True,
            )
        made.append(sym)
        log.info("backfill: reconstructed order row for %s (%g, $%.2f)", sym, qty, cost_basis)

    if made and not dry_run:
        conn.commit()
    if made:
        log.warning(
            "STARTUP GUARD: the broker holds %d position(s) the local database did not "
            "know about — reconstructed as order rows and proceeding on broker truth: %s",
            len(made), ", ".join(made),
        )
    return {"reconstructed": len(made), "symbols": made, "skipped": None}


def sync_with_broker(conn) -> dict:
    """Full startup reconciliation: backfill orphan broker positions, then flip
    local rows the broker cannot confirm. Safe to call on every boot."""
    back = backfill_positions(conn)
    rec = reconcile_orders(conn)
    return {"backfill": back, "reconcile": rec}
