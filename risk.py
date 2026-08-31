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
# These are the strict Phase-3 defaults, set for an equity project with no P&L
# criterion. The hackathon's first judging criterion is realised paper-account
# P&L over ~four trading days, judged by inspecting the account directly — so for
# that window two of them are widened via env vars (D53), exactly as the gate's
# ``min_trades`` is (D50). The defaults in this file are unchanged, so the seed
# lineage / earlier analysis and every test still read them.
MAX_CONCURRENT_POSITIONS = 3
MAX_NOTIONAL_PER_POSITION = 2_000.0   # dollars

# Options (D48). A long option can expire worthless, so the premium paid is the
# whole risk — cap both the size of one position and the total premium exposed
# across every live option position at once.
MAX_OPTION_CONTRACTS_PER_POSITION = 5   # per position — NOT env-overridable (D53)
MAX_TOTAL_OPTION_PREMIUM_AT_RISK = 2_500.0   # dollars, summed over live option orders

# For the competition window only: the strict default above, overridden when the
# matching ``RISK_*`` env var is set to a number (D53). The competition instance
# sets ``RISK_MAX_CONCURRENT_POSITIONS=8`` and
# ``RISK_MAX_OPTION_PREMIUM_AT_RISK=8000`` (~8% of a $100k account); nothing else
# moves. Kill switch, the notional ceiling and the per-position contract cap are
# never env-touched.
_LIMIT_ENV_KEYS: dict[str, str] = {
    "MAX_CONCURRENT_POSITIONS": "RISK_MAX_CONCURRENT_POSITIONS",
    "MAX_TOTAL_OPTION_PREMIUM_AT_RISK": "RISK_MAX_OPTION_PREMIUM_AT_RISK",
}


def limit(name: str) -> float:
    """The limit actually in force: the strict module default, overridden by its
    ``RISK_*`` env var when that is set to a number. A missing or unparseable var
    leaves the strict default. Mirrors ``gate.active_thresholds()`` (D53)."""
    default = globals()[name]
    raw = os.getenv(_LIMIT_ENV_KEYS.get(name, ""), "")
    if not raw.strip():
        return default
    try:
        return type(default)(float(raw))
    except ValueError:
        log.warning("ignoring non-numeric %s=%r — keeping default %s",
                    _LIMIT_ENV_KEYS[name], raw, default)
        return default

# Alpaca order statuses that mean the position/order is no longer live.
_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day",
    "dry_run", "blocked", "error", "skipped",
    "reconciled-closed",  # startup reconciliation found no matching live broker state
}

# A long option is at risk from the moment the buy is accepted until the contract
# leaves the account — so unlike the generic set above, a *filled* option buy is
# still "open" here. It only stops counting once it is sold/closed by the exit
# sweep (``closed``), expires, or the startup reconciliation finds it gone
# (``reconciled-closed``). This keeps the premium and position ceilings honest
# for a live position, not just a working order (D52).
_OPTION_CLOSED_STATUSES = {
    "canceled", "cancelled", "expired", "rejected", "dry_run", "blocked",
    "error", "skipped", "reconciled-closed", "closed",
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


def _open_option_buys(conn) -> list:
    """Option BUY orders that still represent an open long position (see
    ``_OPTION_CLOSED_STATUSES``) — a ``filled`` buy is included, a sold/expired
    one is not."""
    out = []
    for r in db.list_orders(conn):
        keys = r.keys()
        if "asset_class" not in keys or r["asset_class"] != "option":
            continue
        if str(r["side"]).lower() != "buy":
            continue
        if str(r["status"]).lower() in _OPTION_CLOSED_STATUSES:
            continue
        out.append(r)
    return out


def _live_position_strategy_count(conn) -> int:
    rows = db.list_orders(conn)
    live = {
        r["strategy_id"] for r in rows
        if str(r["status"]).lower() not in _TERMINAL_ORDER_STATUSES
    }
    # A filled option buy is terminal as an *order* but still an open *position* —
    # count its strategy until the contract is closed/expired (D52).
    live |= {r["strategy_id"] for r in _open_option_buys(conn)}
    return len(live)


def _live_option_premium_at_risk(conn) -> float:
    """Sum of premium on still-open BUY option positions (dollars exposed)."""
    return sum(float(r["premium"] or 0.0) for r in _open_option_buys(conn))


def check_option(
    conn,
    *,
    contracts: int,
    premium_per_contract: float,
    dry_run: bool | None = None,
) -> RiskDecision:
    """Pre-trade controls for the option expression path (D48).

    ``premium_per_contract`` is the cash to open one contract (ask * 100). Order
    premium at risk = ``contracts * premium_per_contract``; it is added to the
    premium already live on other option positions and checked against
    ``MAX_TOTAL_OPTION_PREMIUM_AT_RISK``. Kill switch and the concurrent-position
    ceiling apply exactly as for stocks; ``DRY_RUN`` is surfaced, not blocking.
    """
    is_dry = dry_run_active(dry_run)

    engaged, why = kill_switch_engaged(conn)
    if engaged:
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    max_positions = limit("MAX_CONCURRENT_POSITIONS")
    live = _live_position_strategy_count(conn)
    if live >= max_positions:
        why = (f"max concurrent positions reached: {live} live "
               f">= {max_positions} allowed")
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    if contracts > MAX_OPTION_CONTRACTS_PER_POSITION:
        why = (f"{contracts} contracts exceeds "
               f"{MAX_OPTION_CONTRACTS_PER_POSITION} per-position limit")
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    max_premium = limit("MAX_TOTAL_OPTION_PREMIUM_AT_RISK")
    order_premium = contracts * premium_per_contract
    already = _live_option_premium_at_risk(conn)
    if already + order_premium > max_premium:
        why = (f"total option premium at risk ${already + order_premium:,.2f} "
               f"(${already:,.2f} live + ${order_premium:,.2f} this order) exceeds "
               f"${max_premium:,.2f} limit")
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    reason = (f"all option risk checks passed ({contracts} contract(s), "
              f"${order_premium:,.2f} premium at risk, ${already:,.2f} already live, "
              f"{live} live position(s))")
    if is_dry:
        reason += " [DRY_RUN — order will not be submitted]"
        log.info("RISK OK but DRY_RUN active — no option order will be sent")
    return RiskDecision(allowed=True, reason=reason, dry_run=is_dry)


def check(conn, *, notional: float, dry_run: bool | None = None) -> RiskDecision:
    """Run every control. Blocking reasons are logged here before returning."""
    is_dry = dry_run_active(dry_run)

    engaged, why = kill_switch_engaged(conn)
    if engaged:
        log.warning("ORDER BLOCKED — %s", why)
        return RiskDecision(allowed=False, reason=why, dry_run=is_dry)

    max_positions = limit("MAX_CONCURRENT_POSITIONS")
    live = _live_position_strategy_count(conn)
    if live >= max_positions:
        why = (f"max concurrent positions reached: {live} live "
               f">= {max_positions} allowed")
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
