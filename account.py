"""Live paper-account state for the dashboard's P&L panel — read-only, cached.

Every other dashboard GET renders from stored rows alone (D6). This one is the
deliberate exception (D54): the hackathon's first judging criterion is realised
paper-account P&L, judged by inspecting the Alpaca account directly, so our own
dashboard has to show the same number. It reaches the account through the Alpaca
**MCP path** (the same ``mcp_client`` the order path uses) — never a direct SDK
client.

To keep page loads and the uptime pinger from spawning the MCP subprocess on
every request, the computed snapshot is cached in the ``system_state`` table for
``ACCOUNT_CACHE_TTL_S`` (default 45s). If a live read fails, the last good
snapshot is served with ``stale=True`` and the timestamp it was taken — the panel
always shows a number, never an error.

Starting balance is a fixed constant: the paper account was opened with
$100,000, and total P&L is ``portfolio_value - STARTING_BALANCE``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

import db

log = logging.getLogger("account")

STARTING_BALANCE = 100_000.0
CACHE_KEY = "cache:account"
CACHE_TTL_S = int(os.getenv("ACCOUNT_CACHE_TTL_S", "45"))

_lock = threading.Lock()


# --- the one network round trip ------------------------------------------------


def _fetch_live() -> dict:
    """Read the account + positions through the Alpaca MCP path. Monkeypatched in tests."""
    import mcp_client

    acct = mcp_client.check_connection()  # get_account_info
    try:
        positions = mcp_client.list_positions()  # get_all_positions
    except Exception as exc:  # noqa: BLE001 — a positions failure must not lose the balance
        log.warning("account: positions read failed (%s) — balances only", exc)
        positions = []
    return {"account": acct, "positions": positions}


# --- pure transformation ------------------------------------------------------


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _compute_snapshot(raw: dict) -> dict:
    acct = raw.get("account") or {}
    portfolio_value = _num(acct.get("portfolio_value") or acct.get("equity"))
    cash = _num(acct.get("cash"))
    pnl_abs = round(portfolio_value - STARTING_BALANCE, 2)
    pnl_pct = round(pnl_abs / STARTING_BALANCE * 100, 4) if STARTING_BALANCE else None

    positions = []
    for p in raw.get("positions") or []:
        positions.append({
            "symbol": p.get("symbol"),
            "asset_class": p.get("asset_class"),
            "qty": _num(p.get("qty")),
            "avg_entry_price": _num(p.get("avg_entry_price") or p.get("cost_basis")),
            "current_price": _num(p.get("current_price") or p.get("asset_current_price")),
            "market_value": _num(p.get("market_value")),
            "unrealized_pl": _num(p.get("unrealized_pl")),
            "unrealized_plpc": round(_num(p.get("unrealized_plpc")) * 100, 2),
        })
    positions.sort(key=lambda r: -abs(r["market_value"]))

    return {
        "available": True,
        "starting_balance": STARTING_BALANCE,
        "portfolio_value": round(portfolio_value, 2),
        "cash": round(cash, 2),
        "total_pnl_abs": pnl_abs,
        "total_pnl_pct": pnl_pct,
        "positions": positions,
        "position_count": len(positions),
        "account_number": acct.get("account_number"),
        "account_status": acct.get("status"),
    }


# --- cache (system_state kv) -------------------------------------------------


def _read_cache(conn) -> dict | None:
    raw = db.get_flag(conn, CACHE_KEY)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return {"data": obj["data"], "at": obj["at"]}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _write_cache(conn, data: dict, at: str) -> None:
    db.set_flag(conn, CACHE_KEY, json.dumps({"data": data, "at": at}))


def _fresh(at: str) -> bool:
    try:
        t = datetime.fromisoformat(at)
    except (ValueError, TypeError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() < CACHE_TTL_S


def _present(cached: dict, *, stale: bool, error: str | None = None) -> dict:
    out = {**cached["data"], "as_of": cached["at"], "stale": stale, "source": "cache"}
    if error:
        out["error"] = error
    return out


# --- public ----------------------------------------------------------------


def snapshot(conn, *, force: bool = False) -> dict:
    """Current account state: portfolio value, cash, P&L vs $100k, open positions.

    Served from the ``system_state`` cache when it is younger than
    ``CACHE_TTL_S``; otherwise re-read live through the MCP path. A live-read
    failure falls back to the last cached snapshot (``stale=True``); with no
    cache at all it returns ``{"available": False, "stale": True}``.
    """
    cached = _read_cache(conn)
    if not force and cached and _fresh(cached["at"]):
        return _present(cached, stale=False)

    with _lock:
        cached = _read_cache(conn)  # another request may have refreshed it while we waited
        if not force and cached and _fresh(cached["at"]):
            return _present(cached, stale=False)
        try:
            data = _compute_snapshot(_fetch_live())
        except Exception as exc:  # noqa: BLE001 — surface the last known value, not an error
            log.warning("account snapshot: live read failed (%s)", exc)
            if cached:
                return _present(cached, stale=True, error=str(exc))
            return {"available": False, "stale": True, "error": str(exc),
                    "starting_balance": STARTING_BALANCE}
        at = db._now()
        _write_cache(conn, data, at)
        return {**data, "as_of": at, "stale": False, "source": "live"}
