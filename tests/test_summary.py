"""summary.py — plain-language dashboard blurb.

The LLM is fed only a facts dict of stored numbers; there is always a fallback;
the result is cached and not regenerated on every read. ``summary._call_llm`` is
monkeypatched in every test that needs it.
"""

import json

import pytest

import db
import summary


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "sum.db")
    yield c
    c.close()


def _activity(conn):
    r = db.start_run(conn)
    sid = db.insert_strategy(conn, name="SPY 5/20 cross", symbol="SPY", schema_json="{}",
                             rationale="ride the 5/20 EMA cross on SPY", source="llm",
                             status="active", dedup_key="k1")
    db.insert_decision(conn, strategy_id=sid, run_id=r, outcome="promoted", reason="passed")
    db.insert_order(conn, strategy_id=sid, run_id=r, symbol="SPY260116C00500000",
                    qty=1, side="buy", status="accepted", broker_order_id="b1",
                    asset_class="option", contract_symbol="SPY260116C00500000",
                    underlying="SPY", strike=500.0, expiry="2026-01-16", premium=420.0,
                    selection_reason="~5% OTM, 45 DTE, ask 4.20")
    db.finish_run(conn, r, n_generated=1, n_promoted=1, n_rejected=0)
    db.insert_scheduler_tick(conn, market_open=True, action="entry-cycle", detail="x", run_id=r)


def test_facts_are_built_from_stored_rows_only(conn):
    _activity(conn)
    f = summary.build_facts(conn)
    assert f["cycles"]["total_runs"] == 1
    assert f["cycles"]["scheduler_entry_cycles"] == 1
    assert f["opened"][0]["underlying"] == "SPY"
    assert f["opened"][0]["why"] == "ride the 5/20 EMA cross on SPY"
    assert f["holding"] and f["holding"][0]["contract"] == "SPY260116C00500000"
    json.dumps(f, default=str)  # must be JSON-serialisable for the prompt


def test_closed_contract_is_not_reported_as_held(conn):
    _activity(conn)
    row = db.list_orders(conn)[0]
    db.insert_order(conn, strategy_id=row["strategy_id"], run_id=row["run_id"],
                    symbol="SPY260116C00500000", qty=1, side="sell", status="filled",
                    asset_class="option", contract_symbol="SPY260116C00500000",
                    underlying="SPY", selection_reason="EXIT — +60% profit target")
    f = summary.build_facts(conn)
    assert f["holding"] == []
    assert f["closed"] and "profit target" in f["closed"][0]["reason"]


def test_llm_sees_only_the_facts_and_the_result_is_cached(conn, monkeypatch):
    _activity(conn)
    seen, calls = {}, []

    def fake(facts):
        calls.append(1)
        seen["facts"] = facts
        return "The agent ran one cycle and opened a SPY call."

    monkeypatch.setattr(summary, "_call_llm", fake)
    a = summary.get_or_generate(conn)
    b = summary.get_or_generate(conn)
    assert a["text"].startswith("The agent ran one cycle")
    assert b["text"] == a["text"] and b["stale"] is False
    assert len(calls) == 1  # second call served from the system_state cache
    assert "SPY" in json.dumps(seen["facts"], default=str)


def test_falls_back_to_plain_text_when_the_llm_fails(conn, monkeypatch):
    _activity(conn)

    def boom(facts):
        raise RuntimeError("no api key")

    monkeypatch.setattr(summary, "_call_llm", boom)
    out = summary.get_or_generate(conn)
    assert out["text"] and "cycle" in out["text"].lower()
    assert "SPY260116C00500000" in out["text"]


def test_empty_database_still_summarises_without_a_call(conn, monkeypatch):
    monkeypatch.setattr(summary, "_call_llm", lambda facts: "")  # empty -> fallback
    out = summary.get_or_generate(conn)
    assert "no open positions" in out["text"].lower()
