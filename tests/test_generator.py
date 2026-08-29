"""generator.py tests — offline only (the API is monkeypatched; the suite never
hits the network).

Covers:
  T2.1  five valid strategies parsed from one call
  T2.2  an invalid item is fed back with its error and recovered; and after
        MAX_RETRIES the leftovers are given up on and logged
  T2.3  near-identical strategies collapse (condition order normalised away),
        and generating twice does not duplicate rows in the DB
"""

import json

import pytest

import db
import generator
from schema import Strategy


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def scripted_llm(monkeypatch):
    """Feed generator._call_llm a scripted list of raw responses.

    The last scripted response repeats if the code calls more times than there
    are responses. ``state["calls"]`` records the messages passed each call.
    """
    state = {"responses": [], "calls": []}

    def fake_call(messages, *, temperature=0.8):
        state["calls"].append(messages)
        idx = min(len(state["calls"]) - 1, len(state["responses"]) - 1)
        return state["responses"][idx]

    monkeypatch.setattr(generator, "_call_llm", fake_call)
    return state


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "gen.db")
    yield c
    c.close()


# --- builders ------------------------------------------------------------


def _one_cond_strategy(symbol="AAPL", name="s", fast=10, slow=30):
    ind = lambda p: {"indicator": "SMA", "period": p}
    return {
        "name": name,
        "symbol": symbol,
        "entry": {"conditions": [{"left": ind(fast), "operator": "CROSSES_ABOVE",
                                  "right": ind(slow)}], "join": None},
        "exit": {"conditions": [{"left": ind(fast), "operator": "CROSSES_BELOW",
                                 "right": ind(slow)}], "join": None},
        "rationale": "trend follow",
    }


def _two_cond_strategy(symbol="AAPL", name="s", swap=False):
    c_rsi = {"left": {"indicator": "RSI", "period": 14}, "operator": "<", "right": 30.0}
    c_sma = {"left": {"indicator": "SMA", "period": 10}, "operator": ">",
             "right": {"indicator": "SMA", "period": 50}}
    conds = [c_sma, c_rsi] if swap else [c_rsi, c_sma]
    return {
        "name": name,
        "symbol": symbol,
        "entry": {"conditions": conds, "join": "AND"},
        "exit": {"conditions": [{"left": {"indicator": "SMA", "period": 10},
                                 "operator": "CROSSES_BELOW",
                                 "right": {"indicator": "SMA", "period": 50}}],
                 "join": None},
        "rationale": "oversold inside an uptrend",
    }


def _batch(*strategies):
    return json.dumps({"strategies": list(strategies)})


def _five_distinct():
    return [_one_cond_strategy(name=f"s{i}", slow=20 + i * 5) for i in range(5)]


# --- T2.1 --------------------------------------------------------------------


def test_five_valid_strategies_from_one_call(scripted_llm):
    scripted_llm["responses"] = [_batch(*_five_distinct())]

    result = generator.generate(n=5)

    assert result.attempts == 1
    assert len(result.strategies) == 5
    assert all(isinstance(s, Strategy) for s in result.strategies)
    assert result.failures == []


# --- T2.2 --------------------------------------------------------------------


def test_invalid_item_is_fed_back_and_recovered(scripted_llm):
    good4 = _five_distinct()[:4]
    broken = _one_cond_strategy(name="bad", slow=999)  # period > MAX_PERIOD
    scripted_llm["responses"] = [
        _batch(*good4, broken),
        _batch(_one_cond_strategy(name="fixed", slow=45)),
    ]

    result = generator.generate(n=5)

    assert result.attempts == 2
    assert len(result.strategies) == 5
    assert result.failures == []
    # the retry turn must carry the specific validation error back to the model
    retry_turn = json.dumps(scripted_llm["calls"][1])
    assert "250" in retry_turn and "period" in retry_turn


def test_gives_up_after_max_retries_and_logs(scripted_llm, caplog):
    good3 = _five_distinct()[:3]
    broken = _one_cond_strategy(name="bad", slow=999)
    scripted_llm["responses"] = [_batch(*good3, broken)]  # repeats every call

    with caplog.at_level("WARNING"):
        result = generator.generate(n=5, max_retries=2)

    assert result.attempts == 3  # 1 initial + 2 retries
    assert len(result.strategies) == 3
    assert len(result.failures) == 1
    assert any("dropped invalid candidate" in r.message for r in caplog.records)


# --- T2.3 --------------------------------------------------------------------


def test_near_identical_strategies_collapse(scripted_llm):
    same_a = _two_cond_strategy(name="oversold-uptrend")
    same_b = _two_cond_strategy(name="RSI dip buy", swap=True)  # conditions reordered
    other = _one_cond_strategy(name="sma-cross")
    scripted_llm["responses"] = [_batch(same_a, same_b, other)]

    result = generator.generate(n=3)

    assert len(result.strategies) == 2
    assert result.duplicates_collapsed == 1


def test_generating_twice_does_not_duplicate_in_db(scripted_llm, conn):
    scripted_llm["responses"] = [_batch(*_five_distinct())]

    first = generator.generate(n=5, conn=conn)
    assert db.count_strategies(conn) == 5
    assert len(first.strategy_ids) == 5

    second = generator.generate(n=5, conn=conn)
    assert db.count_strategies(conn) == 5  # same 5, not 10
    assert sorted(second.strategy_ids) == sorted(first.strategy_ids)


def test_every_persisted_strategy_keeps_its_raw_output(scripted_llm, conn):
    scripted_llm["responses"] = [_batch(*_five_distinct())]

    generator.generate(n=5, conn=conn)

    for row in db.list_strategies(conn):
        assert row["raw_llm_output"] is not None
        assert row["dedup_key"] is not None
        assert row["source"] == "llm"
