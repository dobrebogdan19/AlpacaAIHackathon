"""Post-mortem generation for a retirement (T4.4).

When a shadow retires an active strategy, we store a short written explanation
grounded in the actual numbers: what the retired strategy did in-sample and
forward, what the shadow did, over what window, and what the promotion gate
missed. The LLM writes the prose, but it is fed **only** a facts dict of real
metrics (see ``retire._facts`` / D37). The prompt forbids inventing causes,
market events, or any narrative not present in the numbers.

Provider: OpenAI ``gpt-4o-mini`` (same as ``generator``; one constant). The
single network entry point is :func:`_call_llm` — tests monkeypatch it. If the
call fails or no key is configured, :func:`_fallback_text` renders the same
facts plainly so a retirement is never left without an explanation.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("postmortem")

MODEL = "gpt-4o-mini"

_SYSTEM = (
    "You write short, factual post-mortems for a self-auditing trading agent. "
    "You are given ONLY a JSON object of real backtest numbers for two strategies "
    "on one symbol: the strategy that was promoted at the as-of date and later "
    "retired, and the shadow (rejected) candidate that outperformed it forward. "
    "Write 3-5 sentences that: (1) state what the retired strategy looked like "
    "in-sample (the numbers the gate saw) and what it did forward; (2) state what "
    "the shadow did forward; (3) name what the gate missed — i.e. the in-sample "
    "metric(s) that looked acceptable but did not hold up. "
    "Rules: use only the numbers provided. Do NOT invent market events, causes, "
    "price moves, macro conditions, or any narrative not derivable from these "
    "numbers. Do NOT speculate about why. No headings, no bullet points, plain prose."
)


def _client():
    from openai import OpenAI
    from dotenv import load_dotenv

    load_dotenv()
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (.env)")
    return OpenAI(api_key=key)


def _call_llm(facts: dict) -> str:
    """Single network entry point. Monkeypatched in tests."""
    resp = _client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(facts, sort_keys=True)},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


def _fallback_text(facts: dict) -> str:
    r = facts["retired_strategy"]
    s = facts["promoted_shadow"]
    return (
        f"{facts['symbol']}: '{r['name']}' was promoted on in-sample metrics of "
        f"{r['insample']['total_return_pct']}% return, "
        f"{r['insample']['max_drawdown_pct']}% max drawdown, "
        f"{r['insample']['num_trades']} trades, {r['insample']['win_rate_pct']}% win rate. "
        f"Over the {facts['forward_bars']}-bar forward window it returned "
        f"{r['forward']['total_return_pct']}% while the rejected shadow '{s['name']}' "
        f"returned {s['forward']['total_return_pct']}% "
        f"(shadow in-sample: {s['insample']['total_return_pct']}% return, "
        f"{s['insample']['num_trades']} trades). The gate judged the promoted "
        f"strategy only on its in-sample window; that window's return and trade "
        f"count did not carry into the forward period."
    )


def generate_postmortem(facts: dict) -> str:
    """Return post-mortem prose for a retirement. Falls back to a plain rendering
    of the same facts if the LLM call fails."""
    try:
        text = (_call_llm(facts) or "").strip()
        if text:
            return text
        log.warning("post-mortem LLM returned empty text; using fallback")
    except Exception as exc:  # noqa: BLE001
        log.warning("post-mortem LLM call failed (%s); using fallback", exc)
    return _fallback_text(facts)
