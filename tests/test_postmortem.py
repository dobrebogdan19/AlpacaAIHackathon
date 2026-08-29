"""postmortem.py — the LLM sees only numbers, and there is always a fallback."""

import json

import postmortem

_FACTS = {
    "symbol": "AAPL",
    "forward_bars": 52,
    "policy": {"min_forward_bars": 40, "min_outperformance_pct": 5.0,
               "active_max_forward_return_pct": 0.0},
    "retired_strategy": {
        "name": "AAPL fast cross",
        "insample": {"total_return_pct": 12.0, "max_drawdown_pct": 9.0,
                     "num_trades": 14, "win_rate_pct": 57.0,
                     "open_position": False, "unrealized_pnl_pct": 0.0},
        "forward": {"total_return_pct": -7.5, "max_drawdown_pct": 15.0,
                    "num_trades": 5, "win_rate_pct": 20.0,
                    "open_position": False, "unrealized_pnl_pct": 0.0},
    },
    "promoted_shadow": {
        "name": "AAPL slow cross",
        "insample": {"total_return_pct": 1.0, "max_drawdown_pct": 4.0,
                     "num_trades": 3, "win_rate_pct": 33.0,
                     "open_position": False, "unrealized_pnl_pct": 0.0},
        "forward": {"total_return_pct": 6.2, "max_drawdown_pct": 5.0,
                    "num_trades": 4, "win_rate_pct": 75.0,
                    "open_position": False, "unrealized_pnl_pct": 0.0},
    },
}


def test_llm_is_fed_only_the_facts_json(monkeypatch):
    seen = {}

    def fake_call(facts):
        seen["facts"] = facts
        return "The retired strategy showed 12% in-sample and -7.5% forward."

    monkeypatch.setattr(postmortem, "_call_llm", fake_call)
    text = postmortem.generate_postmortem(_FACTS)

    assert "12" in text
    # exactly the facts dict, nothing else, round-trips as JSON
    assert seen["facts"] == _FACTS
    json.dumps(seen["facts"])


def test_falls_back_to_a_plain_rendering_when_the_call_fails(monkeypatch):
    def boom(facts):
        raise RuntimeError("no api key")

    monkeypatch.setattr(postmortem, "_call_llm", boom)
    text = postmortem.generate_postmortem(_FACTS)

    assert "AAPL" in text
    assert "-7.5%" in text and "6.2%" in text        # real numbers, both sides
    assert "AAPL fast cross" in text


def test_fallback_on_empty_llm_response(monkeypatch):
    monkeypatch.setattr(postmortem, "_call_llm", lambda facts: "   ")
    text = postmortem.generate_postmortem(_FACTS)
    assert "AAPL slow cross" in text
