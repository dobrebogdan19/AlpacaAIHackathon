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
"""

from __future__ import annotations

import logging

import db
import mcp_client

log = logging.getLogger("reconcile")

# Local order statuses that claim an open position/working order. Anything here
# that the broker does not confirm gets flipped to ``reconciled-closed``.
_OPEN_LOCAL_STATUSES = {
    "accepted", "new", "pending_new", "partially_filled", "filled",
    "accepted_for_bidding", "held", "pending_replace", "replaced",
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
