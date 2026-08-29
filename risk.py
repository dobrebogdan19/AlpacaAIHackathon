"""Pre-trade risk controls (T3.3).

``check(...)`` is called immediately before every order submission — there is no
code path to an order that skips it. It returns a :class:`RiskDecision`; if
``allowed`` is False the caller must not submit, and the blocking reason has
already been logged here at WARNING.

Controls, in the order checked:
  1. global kill switch  — env ``KILL_SWITCH`` truthy, or a ``kill_switch`` row
     in the ``system_state`` table set to a truthy string. Either one blocks
     everything.
  2. max concurrent positions — at most ``MAX_CONCURRENT_POSITIONS`` strategies
     may hold a live (non-terminal) order at once.
  3. max notional per position — a single order may not exceed
     ``MAX_NOTIONAL_PER_POSITION`` dollars. Position sizing is fixed notional
     (CLAUDE.md), so this is a sanity ceiling, not a sizing model.

``DRY_RUN`` is honoured everywhere: it is surfaced on the RiskDecision and logged,
and the order path does not submit when it is set. DRY_RUN does not by itself
*block* — a dry run is allowed to proceed through every check so the logs show
what a live run would have done.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import db

log = logging.getLogger("risk")

# --- limits — one place ---------------------------------------------------
MAX_CONCURRENT_POSITIONS = 3
MAX_NOTIONAL_PER_POSITION = 2_000.0   # dollars

# Alpaca order statuses that mean the position/order is no longer live.
_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day",
    "dry_run", "blocked", "error",
}

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    dry_run: bool = False


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def dry_run_active(explicit: bool | None = None) -> bool:
    """DRY_RUN is on if passed explicitly, else if the ``DRY_RUN`` env var is truthy."""
    if explicit is not None:
        return explicit
    return _env_truthy("DRY_RUN")


def kill_switch_engaged(conn) -> tuple[bool, str]:
    """True if the env kill switch or the DB kill-switch flag is set."""
    if _env_truthy("KILL_SWITCH"):
        return True, "kill switch engaged via KILL_SWITCH env var"
    flag = db.get_flag(conn, "kill_switch")
    if flag is not None and flag.strip().lower() in _TRUTHY:
        return True, "kill switch engaged via system_state.kill_switch"
    return False, ""


def _live_position_strategy_count(conn) -> int:
    rows = db.list_orders(conn)
    live = {
        r["strategy_id"] for r in rows
        if str(r["status"]).lower() not in _TERMINAL_ORDER_STATUSES
    }
    return len(live)


def check(conn, *, notional: float, dry_run: bool | None = None) -> RiskDecision:
    """Run every control. Blocking reasons are logged here before returning."""
    is_dry = dry_run_active(dry_run)

    engaged, why = kill_switch_engaged(conn)
    if engaged:
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    live = _live_position_strategy_count(conn)
    if live >= MAX_CONCURRENT_POSITIONS:
        why = (f"max concurrent positions reached: {live} live "
               f">= {MAX_CONCURRENT_POSITIONS} allowed")
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    if notional > MAX_NOTIONAL_PER_POSITION:
        why = (f"notional ${notional:,.2f} exceeds "
               f"${MAX_NOTIONAL_PER_POSITION:,.2f} per-position limit")
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    reason = f"all risk checks passed (notional ${notional:,.2f}, {live} live position(s))"
    if is_dry:
        reason += " [DRY_RUN — order will not be submitted]"
        log.info("RISK OK but DRY_RUN active — no order will be sent")
    return RiskDecision(allowed=True, reason=reason, dry_run=is_dry)
