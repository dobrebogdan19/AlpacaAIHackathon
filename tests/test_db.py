"""db.py tests — T1.5 acceptance: the schema is created on startup and is
idempotent. Plus round-trips for each table and a check that Phase 4's values
(status 'retired', decision outcome 'retired') already fit without a migration.
"""

import json

import pytest

import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {r["name"] for r in rows}


def test_connect_creates_all_tables(conn):
    assert {"strategies", "runs", "backtests", "decisions"} <= _tables(conn)


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    c1 = db.connect(path)
    sid = db.insert_strategy(
        c1, name="s", symbol="AAPL", schema_json="{}", rationale="r", source="manual"
    )
    c1.close()

    # reopening (which re-runs the schema script) must not error or wipe data
    c2 = db.connect(path)
    db.init_db(c2)  # explicit second call, too
    assert db.get_strategy(c2, sid) is not None
    c2.close()


def test_strategy_round_trip(conn):
    sid = db.insert_strategy(
        conn,
        name="AAPL SMA cross",
        symbol="AAPL",
        schema_json=json.dumps({"name": "AAPL SMA cross"}),
        rationale="classic trend follow",
        source="llm",
        raw_llm_output='{"strategies": [...]}',
        dedup_key="abc123",
    )
    row = db.get_strategy(conn, sid)
    assert row["name"] == "AAPL SMA cross"
    assert row["source"] == "llm"
    assert row["status"] == "candidate"
    assert row["raw_llm_output"] == '{"strategies": [...]}'
    assert row["created_at"]


def test_dedup_key_is_unique_and_insert_is_idempotent(conn):
    first = db.insert_strategy(
        conn, name="a", symbol="AAPL", schema_json="{}", rationale=None,
        source="llm", dedup_key="same-key",
    )
    second = db.insert_strategy(
        conn, name="b-different-name", symbol="AAPL", schema_json="{}", rationale=None,
        source="llm", dedup_key="same-key",
    )
    assert first == second
    assert db.count_strategies(conn) == 1


def test_null_dedup_keys_do_not_collide(conn):
    a = db.insert_strategy(conn, name="a", symbol="AAPL", schema_json="{}",
                           rationale=None, source="manual")
    b = db.insert_strategy(conn, name="b", symbol="AAPL", schema_json="{}",
                           rationale=None, source="manual")
    assert a != b
    assert db.count_strategies(conn) == 2


def test_run_lifecycle(conn):
    run_id = db.start_run(conn)
    assert db.get_run(conn, run_id)["finished_at"] is None
    db.finish_run(conn, run_id, n_generated=5, n_promoted=2, n_rejected=3)
    row = db.get_run(conn, run_id)
    assert (row["n_generated"], row["n_promoted"], row["n_rejected"]) == (5, 2, 3)
    assert row["finished_at"]


def test_backtest_and_decision_attach_to_a_strategy(conn):
    run_id = db.start_run(conn)
    sid = db.insert_strategy(conn, name="s", symbol="AAPL", schema_json="{}",
                             rationale=None, source="llm", dedup_key="k")
    bt = db.insert_backtest(
        conn, strategy_id=sid, run_id=run_id,
        metrics_json=json.dumps({"total_return_pct": 3.84}),
        equity_curve_json=json.dumps([{"date": "2026-01-01", "equity": 10000}]),
        bars_start="2025-01-02", bars_end="2026-08-29",
    )
    assert bt > 0
    did = db.insert_decision(conn, strategy_id=sid, run_id=run_id,
                             outcome="promoted", reason="all thresholds passed")
    assert db.list_decisions(conn)[0]["id"] == did


def test_phase4_values_fit_without_migration(conn):
    """A retirement (Phase 4) is status='retired' + a 'retired' decision row."""
    sid = db.insert_strategy(conn, name="s", symbol="AAPL", schema_json="{}",
                             rationale=None, source="llm", dedup_key="k", status="active")
    db.set_strategy_status(conn, sid, "retired")
    assert db.get_strategy(conn, sid)["status"] == "retired"
    db.insert_decision(conn, strategy_id=sid, run_id=None,
                       outcome="retired", reason="shadow beat it over 60 bars")
    assert db.list_decisions(conn)[-1]["outcome"] == "retired"


def test_bad_enum_values_are_rejected(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_strategy(conn, name="s", symbol="AAPL", schema_json="{}",
                           rationale=None, source="not-a-source")


def test_order_reconstructed_flag_round_trips(conn):
    """D58: the reconstructed marker on an order row defaults 0 and stores 1."""
    sid = db.insert_strategy(conn, name="s", symbol="AAPL", schema_json="{}",
                             rationale=None, source="manual", dedup_key="k")
    plain = db.insert_order(conn, strategy_id=sid, run_id=None, symbol="AAPL",
                            qty=1.0, side="buy", status="filled")
    recon = db.insert_order(conn, strategy_id=sid, run_id=None, symbol="AAPL",
                            qty=1.0, side="buy", status="broker-reconstructed",
                            reconstructed=True)
    rows = {o["id"]: o["reconstructed"] for o in db.list_orders(conn)}
    assert rows[plain] == 0 and rows[recon] == 1
