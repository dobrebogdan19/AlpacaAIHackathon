"""Plain-language summary of what the agent has done recently (dashboard).

The dashboard is an engineer's readout — decision tables, metrics, order rows.
This turns the last stretch of stored history into a few plain sentences: how
many cycles ran, what was bought and why, what was closed and why, and what is
held now.

The LLM writes the prose but is handed **only** a facts dict of real stored
numbers (same discipline as ``postmortem.py`` / D37). The system prompt forbids
inventing anything not in the numbers, and :func:`_fallback_text` renders the
same facts plainly when the call fails or no key is configured.

Generation costs a call and the content barely moves between page loads, so the
result is cached in ``system_state`` and regenerated at most once an hour
(``SUMMARY_MAX_AGE_S``). A read that finds a fresh cache never calls the LLM.

Provider: OpenAI ``gpt-4o-mini`` (one constant, as in ``generator`` /
``postmortem``). The single network entry point is :func:`_call_llm`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

import db

log = logging.getLogger("summary")

MODEL = "gpt-4o-mini"
CACHE_KEY = "cache:summary"
MAX_AGE_S = int(os.getenv("SUMMARY_MAX_AGE_S", "3600"))
_RECENT = 8  # cap on how many opened/closed rows go into the facts dict

_lock = threading.Lock()

# Order statuses that mean a bought contract is no longer an open position.
# Mirrors ``risk._OPTION_CLOSED_STATUSES`` (D52) — kept local so this display
# module has no dependency on the risk engine.
_CLOSED_LIKE = {
    "canceled", "cancelled", "expired", "rejected", "dry_run", "blocked",
    "error", "skipped", "reconciled-closed", "closed",
}

_SYSTEM = (
    "You write a short, factual status note for a self-auditing paper-trading "
    "agent's dashboard. You are given ONLY a JSON object of real numbers taken "
    "from the agent's own database. Write 3-5 plain sentences covering: how many "
    "cycles it has run, what it has opened recently and the stated reason, what "
    "it has closed and why, and what it is holding now. "
    "Rules: use only the numbers provided. Do NOT invent tickers, prices, dates, "
    "market events, or performance claims not present in the data. Do NOT "
    "editorialise about whether it is doing well. If a category is empty, say so "
    "briefly. If 'holding_reconstructed' is above zero, state plainly that that "
    "many held positions were rebuilt from the broker after a restart wiped the "
    "local records, so the positions are real but the strategy/decision history "
    "for them was lost. No headings, no bullet points, no hype — plain prose."
)


# --- network entry point --------------------------------------------------


def _call_llm(facts: dict) -> str:
    """Single network entry point. Monkeypatched in tests."""
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (.env)")
    resp = OpenAI(api_key=key).chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(facts, sort_keys=True, default=str)},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


# --- facts, from stored rows only ---------------------------------------------


def _contract_fields(o: dict) -> dict:
    if o.get("contract_symbol"):
        return {"underlying": o.get("underlying"), "contract": o.get("contract_symbol"),
                "strike": o.get("strike"), "expiry": o.get("expiry"),
                "premium_usd": o.get("premium")}
    return {"symbol": o.get("symbol")}


def build_facts(conn) -> dict:
    runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()]
    live_runs = [r for r in runs if r.get("as_of") is None]
    recent_runs = [{
        "run_id": r["id"], "started_at": r["started_at"],
        "generated": r["n_generated"], "promoted": r["n_promoted"],
        "rejected": r["n_rejected"], "finished": bool(r["finished_at"]),
    } for r in live_runs[:6]]

    tick_counts = {row["action"]: row["n"] for row in conn.execute(
        "SELECT action, COUNT(*) AS n FROM scheduler_ticks GROUP BY action")}

    orders = [dict(r) for r in conn.execute(
        """SELECT o.*, s.name AS strategy_name, s.rationale AS strategy_rationale
             FROM orders o JOIN strategies s ON s.id = o.strategy_id
            ORDER BY o.id DESC""").fetchall()]

    buys = [o for o in orders if str(o["side"]).lower() == "buy"]
    sells = [o for o in orders if str(o["side"]).lower() == "sell"]
    sold_contracts = {o.get("contract_symbol") for o in sells if o.get("contract_symbol")}

    opened = [{
        "when": o["created_at"], "strategy": o["strategy_name"],
        "why": o["strategy_rationale"], "status": o["status"],
        "contract_choice": o.get("selection_reason"),
        **_contract_fields(o),
    } for o in buys[:_RECENT]]

    closed = [{
        "when": o["created_at"], "strategy": o["strategy_name"],
        "status": o["status"], "reason": o.get("selection_reason"),
        **_contract_fields(o),
    } for o in sells[:_RECENT]]

    held_rows = [o for o in buys
                 if str(o["status"]).lower() not in _CLOSED_LIKE
                 and o.get("contract_symbol") not in sold_contracts]
    holding = [{
        "since": o["created_at"], "strategy": o["strategy_name"],
        "reconstructed": bool(o["reconstructed"]) if "reconstructed" in o.keys() else False,
        **_contract_fields(o),
    } for o in held_rows]
    holding_reconstructed = sum(1 for h in holding if h["reconstructed"])

    retirements = [dict(r) for r in conn.execute(
        """SELECT p.created_at, r.name AS retired, w.name AS replacement
             FROM postmortems p
             JOIN strategies r ON r.id = p.retired_strategy_id
             JOIN strategies w ON w.id = p.promoted_strategy_id
            ORDER BY p.id DESC""").fetchall()]

    return {
        "as_of": db._now(),
        "starting_balance_usd": 100_000,
        "cycles": {
            "total_runs": len(live_runs),
            "finished_runs": sum(1 for r in live_runs if r["finished_at"]),
            "scheduler_entry_cycles": tick_counts.get("entry-cycle", 0),
            "scheduler_ticks_total": sum(tick_counts.values()),
            "last_entry_cycle_at": db.last_entry_cycle_at(conn),
            "recent_runs": recent_runs,
        },
        "opened": opened,
        "closed": closed,
        "holding": holding,
        "holding_reconstructed": holding_reconstructed,
        "retirements": retirements,
        "note_to_writer": (
            "These are the only facts. State nothing not present here. Empty list "
            "means that category had no activity."
        ),
    }


# --- prose ---------------------------------------------------------------------


def _fallback_text(facts: dict) -> str:
    c = facts["cycles"]
    parts = [
        f"The agent has run {c['scheduler_entry_cycles']} scheduled entry "
        f"cycle(s) across {c['scheduler_ticks_total']} scheduler tick(s), "
        f"{c['total_runs']} run(s) in total."
    ]
    if facts["opened"]:
        shown = facts["opened"][:3]
        bits = []
        for o in shown:
            what = o.get("underlying") or o.get("symbol") or "position"
            kind = "call" if o.get("contract") else "shares"
            bits.append(f"{what} ({kind}, {o['status']})")
        parts.append("Recently opened: " + "; ".join(bits) + ".")
    else:
        parts.append("It has not opened any positions yet.")
    if facts["closed"]:
        parts.append(f"{len(facts['closed'])} position(s) have been closed.")
    if facts["holding"]:
        held = ", ".join(h.get("contract") or h.get("symbol") or "?" for h in facts["holding"])
        parts.append(f"Currently holding: {held}.")
        recon = facts.get("holding_reconstructed", 0)
        if recon:
            parts.append(
                f"{recon} of those position(s) were rebuilt from the broker after a "
                f"restart wiped the local records — the positions are real but the "
                f"strategy and gate reason that opened them were lost."
            )
    else:
        parts.append("It holds no open positions right now.")
    if facts["retirements"]:
        parts.append(
            f"{len(facts['retirements'])} strategy(ies) have been retired after a "
            f"rejected shadow beat them forward."
        )
    return " ".join(parts)


def generate(facts: dict) -> str:
    """Post-mortem-style: try the LLM, fall back to a plain rendering of the facts."""
    try:
        text = (_call_llm(facts) or "").strip()
        if text:
            return text
        log.warning("summary LLM returned empty text; using fallback")
    except Exception as exc:  # noqa: BLE001
        log.warning("summary LLM call failed (%s); using fallback", exc)
    return _fallback_text(facts)


# --- cache + public ----------------------------------------------------------


def _read_cache(conn) -> dict | None:
    raw = db.get_flag(conn, CACHE_KEY)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return {"text": obj["text"], "at": obj["at"]}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _age(at: str) -> float:
    try:
        t = datetime.fromisoformat(at)
    except (ValueError, TypeError):
        return float("inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def get_or_generate(conn, *, force: bool = False) -> dict:
    """Return ``{"text", "generated_at", "stale"}``.

    Serves the cached note when it is younger than ``MAX_AGE_S``; otherwise
    rebuilds the facts from storage and regenerates. A generation failure with a
    cache present returns the stale cache; with no cache it returns the plain
    fallback rendering.
    """
    cached = _read_cache(conn)
    if not force and cached and _age(cached["at"]) < MAX_AGE_S:
        return {"text": cached["text"], "generated_at": cached["at"], "stale": False}

    with _lock:
        cached = _read_cache(conn)
        if not force and cached and _age(cached["at"]) < MAX_AGE_S:
            return {"text": cached["text"], "generated_at": cached["at"], "stale": False}
        try:
            text = generate(build_facts(conn))
        except Exception as exc:  # noqa: BLE001 — never 500 the dashboard over a summary
            log.warning("summary build failed (%s)", exc)
            if cached:
                return {"text": cached["text"], "generated_at": cached["at"], "stale": True}
            raise
        at = db._now()
        db.set_flag(conn, CACHE_KEY, json.dumps({"text": text, "at": at}))
        return {"text": text, "generated_at": at, "stale": False}
