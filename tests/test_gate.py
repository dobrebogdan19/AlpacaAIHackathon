"""gate.py tests (T3.1).

Acceptance: every candidate gets a written reason that names the specific
threshold(s) it failed; the reason is never empty, promote or reject.
"""

import pytest

import gate
from gate import GATE_THRESHOLDS, evaluate


@pytest.fixture(autouse=True)
def _clear_gate_env(monkeypatch):
    for env in ("GATE_MIN_TRADES", "GATE_MIN_TOTAL_RETURN_PCT", "GATE_MAX_DRAWDOWN_PCT"):
        monkeypatch.delenv(env, raising=False)


def _metrics(total=5.0, dd=10.0, trades=12):
    return {
        "total_return_pct": total,
        "max_drawdown_pct": dd,
        "num_trades": trades,
        "win_rate_pct": 50.0,
        "open_position": False,
        "unrealized_pnl_pct": 0.0,
    }


def test_min_trades_threshold_is_ten():
    assert GATE_THRESHOLDS["min_trades"] >= 10


def test_promote_when_all_thresholds_pass():
    r = evaluate(_metrics(total=5.0, dd=10.0, trades=12))
    assert r.promoted is True
    assert r.failed == []
    assert "all thresholds passed" in r.reason
    assert r.reason  # never empty


def test_reject_names_every_failed_threshold():
    r = evaluate(_metrics(total=-3.0, dd=40.0, trades=2))
    assert r.promoted is False
    assert set(r.failed) == {"min_total_return_pct", "max_drawdown_pct", "min_trades"}
    assert "total return -3.00%" in r.reason
    assert "max drawdown 40.00%" in r.reason
    assert "only 2 realised trade(s)" in r.reason


def test_three_trades_now_fails_the_gate():
    """The Phase 1 TODO: 3 trades over ~250 bars is noise; min_trades=10 rejects it."""
    r = evaluate(_metrics(total=3.84, dd=10.78, trades=3))
    assert r.promoted is False
    assert r.failed == ["min_trades"]
    assert "10 required" in r.reason


def test_exactly_at_thresholds_passes():
    r = evaluate(_metrics(total=0.0, dd=25.0, trades=10))
    assert r.promoted is True


def test_custom_thresholds_are_honoured():
    lenient = {"min_total_return_pct": -100.0, "max_drawdown_pct": 100.0, "min_trades": 1}
    assert evaluate(_metrics(total=-5.0, dd=50.0, trades=1), lenient).promoted is True


def test_reason_is_written_for_both_outcomes():
    assert evaluate(_metrics()).reason.strip()
    assert evaluate(_metrics(trades=0)).reason.strip()


# --- env-overridable active thresholds (D50) ------------------------------


def test_active_thresholds_default_to_the_strict_dict():
    assert gate.active_thresholds() == GATE_THRESHOLDS


def test_gate_min_trades_env_loosens_only_min_trades(monkeypatch):
    monkeypatch.setenv("GATE_MIN_TRADES", "3")
    t = gate.active_thresholds()
    assert t["min_trades"] == 3
    assert t["min_total_return_pct"] == GATE_THRESHOLDS["min_total_return_pct"]
    assert t["max_drawdown_pct"] == GATE_THRESHOLDS["max_drawdown_pct"]
    # a 3-trade backtest that clears the other bars now promotes
    assert evaluate(_metrics(total=3.84, dd=10.78, trades=3)).promoted is True


def test_explicit_thresholds_still_win_over_env(monkeypatch):
    monkeypatch.setenv("GATE_MIN_TRADES", "3")
    strict = {"min_total_return_pct": 0.0, "max_drawdown_pct": 25.0, "min_trades": 10}
    assert evaluate(_metrics(trades=3), strict).promoted is False


def test_non_numeric_env_is_ignored(monkeypatch):
    monkeypatch.setenv("GATE_MIN_TRADES", "lots")
    assert gate.active_thresholds()["min_trades"] == GATE_THRESHOLDS["min_trades"]
