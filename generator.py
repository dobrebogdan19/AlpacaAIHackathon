"""Candidate-strategy generation via an LLM, constrained to the closed grammar.

The model never writes code (DECISIONS.md D3). It emits JSON that we validate
through the Pydantic models in ``schema.py``; anything that fails is fed back
with its specific validation error and regenerated (max ``MAX_RETRIES`` times),
then given up on and logged.

Provider: OpenAI (``gpt-4o-mini`` by default — one constant, easy to change).
The API key is read from ``.env`` (``OPENAI_API_KEY``). The single network entry
point is :func:`_call_llm`; tests monkeypatch it and never touch the network.

Pipeline (T2.1 / T2.2 / T2.3):
  1. one call asks for N strategies as JSON, each with a short rationale
  2. validate every item; retry the invalid ones with their error text
  3. deduplicate — collapse strategies with the same symbol and semantically
     equivalent entry/exit rules (condition order within a rule is normalised
     away). See :func:`dedup_key`.
  4. persist every surviving strategy with its raw LLM output kept for audit
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from pydantic import ValidationError

from schema import (
    MAX_PERIOD,
    MIN_PERIOD,
    IndicatorName,
    IndicatorRef,
    Join,
    Operator,
    Strategy,
)

log = logging.getLogger("generator")

MODEL = "gpt-4o-mini"
DEFAULT_N = 5
MAX_RETRIES = 2
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "SPY", "TSLA"]

_client = None


def _openai():
    global _client
    if _client is None:
        from openai import OpenAI  # imported lazily so the suite needn't have it configured

        load_dotenv()
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set (.env)")
        _client = OpenAI(api_key=key)
    return _client


# --- the grammar, spelled out for the model --------------------------------
# Built from the schema enums so it can never drift from what the engine accepts.

_GRAMMAR_CONTRACT = f"""\
You design trading strategies as DATA in a closed grammar. You never write code.

Allowed indicators (field "indicator"): {", ".join(i.value for i in IndicatorName)}
Allowed operators (field "operator"):   {", ".join(o.value for o in Operator)}
Allowed joins (field "join"):           {", ".join(j.value for j in Join)}
Indicator "period": integer, {MIN_PERIOD}..{MAX_PERIOD} inclusive.

A "condition" is an object:
  {{"left": {{"indicator": <name>, "period": <int>}},
   "operator": <operator>,
   "right": {{"indicator": <name>, "period": <int>}}   // OR a bare number, e.g. 70}}

A "rule" is an object:
  {{"conditions": [<condition>], "join": null}}                       // exactly 1 condition
  {{"conditions": [<condition>, <condition>], "join": "AND" | "OR"}}  // exactly 2 conditions
No other shapes. A 1-condition rule must have "join": null. A 2-condition rule
must set "join". Never 3+ conditions. No extra fields anywhere.

">" and "<" compare current-bar values. "CROSSES_ABOVE" / "CROSSES_BELOW" compare
the previous and current bar (a crossover event, not a level).

A "strategy" is an object:
  {{"name": <short string>,
   "symbol": <ticker string>,
   "entry": <rule>,     // when to open the long position
   "exit": <rule>,      // when to close it
   "rationale": <one or two sentences on why this might work>}}
The entry rule and the exit rule MUST differ.

Return ONLY a JSON object of the form:
  {{"strategies": [<strategy>, <strategy>, ...]}}
No prose, no markdown fences — JSON only.
"""


def _system_message() -> dict:
    return {"role": "system", "content": _GRAMMAR_CONTRACT}


def _user_message(n: int, symbols: list[str]) -> dict:
    return {
        "role": "user",
        "content": (
            f"Generate {n} DISTINCT long-only strategies as JSON. "
            f"Pick symbols from: {', '.join(symbols)}. "
            "Vary the indicators, periods, and structure — do not just rescale one idea. "
            "Some strategies should use a 2-condition entry. Keep rationales short."
        ),
    }


def _retry_message(failures: list[dict]) -> dict:
    n = len(failures)
    lines = [
        f"{n} of the strategies were INVALID and were discarded. "
        f"Return {n} corrected strateg{'y' if n == 1 else 'ies'} "
        'as JSON ({"strategies": [...]}), fixing exactly these problems:'
    ]
    for i, f in enumerate(failures, 1):
        lines.append(f"\n[{i}] offending strategy: {json.dumps(f['raw'])}")
        lines.append(f"    validation error: {f['error']}")
    return {"role": "user", "content": "\n".join(lines)}


# --- the one network call --------------------------------------------------


def _call_llm(messages: list[dict], *, temperature: float = 0.8) -> str:
    """Single network entry point. Returns the raw response text. Monkeypatched in tests."""
    resp = _openai().chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""


# --- parsing / validation -------------------------------------------------


def _short_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _parse_batch(raw: str) -> tuple[list[Strategy], list[dict]]:
    """Split a raw LLM response into (valid strategies, invalid items + errors)."""
    valid: list[Strategy] = []
    invalid: list[dict] = []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [{"raw": raw, "error": f"response was not valid JSON: {exc}"}]

    items = payload.get("strategies") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return [], [{"raw": raw, "error": 'expected {"strategies": [ ... ]}'}]

    for item in items:
        try:
            valid.append(Strategy.model_validate(item))
        except ValidationError as exc:
            invalid.append({"raw": item, "error": _short_error(exc)})
    return valid, invalid


# --- deduplication (T2.3) ------------------------------------------------


def _side_key(side) -> tuple:
    if isinstance(side, IndicatorRef):
        return ("ind", side.indicator.value, side.period)
    return ("const", float(side))


def _condition_key(cond) -> tuple:
    return (_side_key(cond.left), cond.operator.value, _side_key(cond.right))


def _rule_key(rule) -> tuple:
    # sort the conditions so that AND/OR operand ordering never creates a "new" strategy
    return (rule.join.value if rule.join else None,
            tuple(sorted((_condition_key(c) for c in rule.conditions))))


def canonical_form(strategy: Strategy) -> dict:
    """Symbol + semantic entry/exit rules, with condition ordering normalised away.

    Name and rationale are deliberately excluded — two strategies that trade the
    same symbol on the same rules are the same strategy however they are labelled.
    """
    return {
        "symbol": strategy.symbol.upper(),
        "entry": _rule_key(strategy.entry),
        "exit": _rule_key(strategy.exit),
    }


def dedup_key(strategy: Strategy) -> str:
    blob = json.dumps(canonical_form(strategy), sort_keys=True, default=list)
    return hashlib.sha256(blob.encode()).hexdigest()


# --- result -------------------------------------------------------------------


@dataclass
class GenerationResult:
    strategies: list[Strategy]          # unique, valid
    strategy_ids: list[int] = field(default_factory=list)   # DB ids, when persisted
    failures: list[dict] = field(default_factory=list)      # still-invalid after retries
    raw_outputs: list[str] = field(default_factory=list)    # every raw LLM response
    attempts: int = 0
    duplicates_collapsed: int = 0


# --- orchestration -------------------------------------------------------


def generate(
    n: int = DEFAULT_N,
    *,
    symbols: list[str] | None = None,
    conn=None,
    run_id: int | None = None,
    max_retries: int = MAX_RETRIES,
    temperature: float = 0.8,
) -> GenerationResult:
    """Generate up to ``n`` unique, valid strategies.

    If ``conn`` (a ``db`` connection) is given, every surviving strategy is
    persisted (``source='llm'``, ``status='candidate'``) with its raw LLM output;
    duplicates already in the database are not re-inserted (T2.3).
    """
    symbols = symbols or DEFAULT_SYMBOLS
    messages = [_system_message(), _user_message(n, symbols)]

    seen: dict[str, Strategy] = {}
    raw_by_key: dict[str, str] = {}
    raw_outputs: list[str] = []
    duplicates = 0
    failures: list[dict] = []
    attempt = 0

    while True:
        raw = _call_llm(messages, temperature=temperature)
        raw_outputs.append(raw)
        attempt += 1
        valid, invalid = _parse_batch(raw)

        for strat in valid:
            key = dedup_key(strat)
            if key in seen:
                duplicates += 1
                continue
            seen[key] = strat
            raw_by_key[key] = raw

        failures = invalid
        # Retries exist only to recover INVALID output (T2.2). If the model
        # simply returned fewer unique strategies than asked, we take what we
        # got rather than churn the API chasing the count.
        if not invalid or attempt > max_retries:
            break
        messages = messages + [
            {"role": "assistant", "content": raw},
            _retry_message(invalid),
        ]

    if failures:
        for f in failures:
            log.warning("dropped invalid candidate after %d attempt(s): %s | %s",
                        attempt, f["error"], json.dumps(f["raw"])[:300])

    strategies = list(seen.values())[:n]
    result = GenerationResult(
        strategies=strategies,
        failures=failures,
        raw_outputs=raw_outputs,
        attempts=attempt,
        duplicates_collapsed=duplicates,
    )

    if conn is not None:
        import db

        for strat in strategies:
            key = dedup_key(strat)
            sid = db.insert_strategy(
                conn,
                name=strat.name,
                symbol=strat.symbol.upper(),
                schema_json=strat.model_dump_json(),
                rationale=strat.rationale,
                source="llm",
                status="candidate",
                raw_llm_output=raw_by_key.get(key),
                dedup_key=key,
            )
            result.strategy_ids.append(sid)

    return result
