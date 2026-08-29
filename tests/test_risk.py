"""risk.py tests (T3.3).

Acceptance: flipping the kill switch blocks orders with a logged reason. Also
covers the position-count ceiling, the per-position notional ceiling, and that
DRY_RUN is surfaced (but does not itself block).
"""

import pytest

import db
import risk


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "risk.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)


def _strategy(conn, name):
    return db.insert_strategy(conn, name=name, symbol="AAPL", schema_json="{}",
                              rationale=None, source="llm", dedup_key=name)


# --- kill switch --------------------------------------------------------------


def test_kill_switch_env_blocks_with_logged_reason(conn, monkeypatch, caplog):
    monkeypatch.setenv("KILL_SWITCH", "1")
    with caplog.at_level("WARNING"):
        d = risk.check(conn, notional=100.0)
    assert d.allowed is False
    assert "kill switch" in d.reason.lower()
    assert any("ORDER BLOCKED" in r.message for r in caplog.records)


def test_kill_switch_db_flag_blocks(conn, caplog):
    db.set_flag(conn, "kill_switch", "true")
    with caplog.at_level("WARNING"):
        d = risk.check(conn, notional=100.0)
    assert d.allowed is False
    assert "system_state" in d.reason
    assert any("ORDER BLOCKED" in r.message for r in caplog.records)


def test_kill_switch_off_by_default(conn):
    assert risk.check(conn, notional=100.0).allowed is True


# --- position count ----------------------------------------------------------


def test_max_concurrent_positions_blocks(conn, caplog):
    for i in range(risk.MAX_CONCURRENT_POSITIONS):
        sid = _strategy(conn, f"s{i}")
        db.insert_order(conn, strategy_id=sid, run_id=None, symbol="AAPL", qty=1.0,
                        side="buy", status="new", broker_order_id=f"o{i}")
    with caplog.at_level("WARNING"):
        d = risk.check(conn, notional=100.0)
    assert d.allowed is False
    assert "max concurrent positions" in d.reason


def test_terminal_orders_do_not_count_against_the_limit(conn):
    for i in range(5):
        sid = _strategy(conn, f"s{i}")
        db.insert_order(conn, strategy_id=sid, run_id=None, symbol="AAPL", qty=1.0,
                        side="buy", status="filled", broker_order_id=f"o{i}")
    assert risk.check(conn, notional=100.0).allowed is True


# --- notional ceiling --------------------------------------------------------


def test_notional_over_limit_blocks(conn, caplog):
    with caplog.at_level("WARNING"):
        d = risk.check(conn, notional=risk.MAX_NOTIONAL_PER_POSITION + 1)
    assert d.allowed is False
    assert "per-position limit" in d.reason


def test_notional_at_limit_passes(conn):
    assert risk.check(conn, notional=risk.MAX_NOTIONAL_PER_POSITION).allowed is True


# --- dry run ---------------------------------------------------------------


def test_dry_run_is_allowed_but_flagged(conn):
    d = risk.check(conn, notional=100.0, dry_run=True)
    assert d.allowed is True
    assert d.dry_run is True
    assert "DRY_RUN" in d.reason


def test_dry_run_env_var_is_picked_up(conn, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    assert risk.dry_run_active() is True
    assert risk.check(conn, notional=100.0).dry_run is True


def test_kill_switch_beats_dry_run(conn, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "yes")
    d = risk.check(conn, notional=100.0, dry_run=True)
    assert d.allowed is False
