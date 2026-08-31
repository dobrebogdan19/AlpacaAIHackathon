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
    monkeypatch.delenv("RISK_MAX_CONCURRENT_POSITIONS", raising=False)
    monkeypatch.delenv("RISK_MAX_OPTION_PREMIUM_AT_RISK", raising=False)


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


# --- option controls (D48) -------------------------------------------------


def _option_order(conn, name, *, premium, status="accepted"):
    sid = _strategy(conn, name)
    db.insert_order(conn, strategy_id=sid, run_id=None, symbol="AAPL260101C00150000",
                    qty=1.0, side="buy", status=status, broker_order_id=f"o-{name}",
                    asset_class="option", contract_symbol="AAPL260101C00150000",
                    underlying="AAPL", premium=premium)


def test_check_option_passes_within_limits(conn):
    d = risk.check_option(conn, contracts=1, premium_per_contract=300.0)
    assert d.allowed is True
    assert "premium at risk" in d.reason


def test_check_option_kill_switch_blocks(conn, monkeypatch, caplog):
    monkeypatch.setenv("KILL_SWITCH", "1")
    with caplog.at_level("WARNING"):
        d = risk.check_option(conn, contracts=1, premium_per_contract=100.0)
    assert d.allowed is False
    assert any("ORDER BLOCKED" in r.message for r in caplog.records)


def test_check_option_contract_count_ceiling_blocks(conn):
    d = risk.check_option(
        conn, contracts=risk.MAX_OPTION_CONTRACTS_PER_POSITION + 1,
        premium_per_contract=10.0,
    )
    assert d.allowed is False
    assert "per-position limit" in d.reason


def test_check_option_total_premium_at_risk_blocks(conn):
    # already live: premium just under the ceiling
    _option_order(conn, "live1", premium=risk.MAX_TOTAL_OPTION_PREMIUM_AT_RISK - 100.0)
    d = risk.check_option(conn, contracts=1, premium_per_contract=250.0)
    assert d.allowed is False
    assert "total option premium at risk" in d.reason


def test_check_option_terminal_orders_do_not_count_premium(conn):
    _option_order(conn, "done1", premium=99_999.0, status="expired")
    d = risk.check_option(conn, contracts=1, premium_per_contract=250.0)
    assert d.allowed is True


def test_check_option_dry_run_flagged_not_blocked(conn):
    d = risk.check_option(conn, contracts=1, premium_per_contract=100.0, dry_run=True)
    assert d.allowed is True and d.dry_run is True
    assert "DRY_RUN" in d.reason


def test_filled_option_buy_still_counts_as_premium_at_risk(conn):
    """A long option is at risk until the contract leaves the account — a
    'filled' buy still counts (D52), unlike an equity fill."""
    _option_order(conn, "held", premium=risk.MAX_TOTAL_OPTION_PREMIUM_AT_RISK - 50.0,
                  status="filled")
    d = risk.check_option(conn, contracts=1, premium_per_contract=200.0)
    assert d.allowed is False
    assert "total option premium at risk" in d.reason


def test_closed_option_buy_frees_the_premium(conn):
    _option_order(conn, "sold", premium=99_999.0, status="closed")
    d = risk.check_option(conn, contracts=1, premium_per_contract=200.0)
    assert d.allowed is True


def test_filled_option_buy_counts_toward_concurrent_positions(conn):
    for i in range(risk.MAX_CONCURRENT_POSITIONS):
        _option_order(conn, f"opt{i}", premium=50.0, status="filled")
    d = risk.check_option(conn, contracts=1, premium_per_contract=10.0)
    assert d.allowed is False
    assert "max concurrent positions" in d.reason


# --- competition-window cap overrides (D53) -------------------------------


def test_limit_defaults_to_the_strict_module_value(conn):
    assert risk.limit("MAX_CONCURRENT_POSITIONS") == risk.MAX_CONCURRENT_POSITIONS
    assert risk.limit("MAX_TOTAL_OPTION_PREMIUM_AT_RISK") == risk.MAX_TOTAL_OPTION_PREMIUM_AT_RISK


def test_risk_env_var_widens_the_concurrent_position_cap(conn, monkeypatch):
    monkeypatch.setenv("RISK_MAX_CONCURRENT_POSITIONS", "8")
    for i in range(risk.MAX_CONCURRENT_POSITIONS):  # 3 live positions — over the strict cap
        sid = _strategy(conn, f"s{i}")
        db.insert_order(conn, strategy_id=sid, run_id=None, symbol="AAPL", qty=1.0,
                        side="buy", status="new", broker_order_id=f"o{i}")
    assert risk.limit("MAX_CONCURRENT_POSITIONS") == 8
    assert risk.check(conn, notional=100.0).allowed is True   # would block at the default 3


def test_risk_env_var_widens_the_option_premium_cap(conn, monkeypatch):
    monkeypatch.setenv("RISK_MAX_OPTION_PREMIUM_AT_RISK", "8000")
    _option_order(conn, "live1", premium=4_000.0)             # over the strict $2,500
    d = risk.check_option(conn, contracts=1, premium_per_contract=1_000.0)
    assert d.allowed is True


def test_non_numeric_risk_env_var_keeps_the_strict_default(conn, monkeypatch):
    monkeypatch.setenv("RISK_MAX_CONCURRENT_POSITIONS", "lots")
    assert risk.limit("MAX_CONCURRENT_POSITIONS") == risk.MAX_CONCURRENT_POSITIONS
