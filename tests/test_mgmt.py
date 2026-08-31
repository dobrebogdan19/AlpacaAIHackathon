"""mgmt.py tests (T5.3 — the option-position exit path).

The exit rule is pure logic; the sweep's broker reads and MCP close are faked.
Focus: a position that hits a rule is closed and recorded; one that does not is
held and recorded; a broker read failure is surfaced, not raised.
"""

import pytest

import db
import mgmt


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "mgmt.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("KILL_SWITCH", raising=False)


@pytest.fixture(autouse=True)
def _stub_order_history(monkeypatch):
    """The sweep reads broker order history for holding age (D60); default it to
    empty so the existing tests never touch the MCP path. Time-stop tests
    override it."""
    monkeypatch.setattr(mgmt.mcp_client, "list_recent_orders",
                        lambda limit=100: [], raising=False)


def _iso_days_ago(days: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# --- evaluate_exit (pure) ------------------------------------------------


def test_profit_target_closes():
    close, why = mgmt.evaluate_exit(pnl_pct=75.0, dte=30)
    assert close and "profit target" in why


def test_stop_loss_closes():
    close, why = mgmt.evaluate_exit(pnl_pct=-60.0, dte=30)
    assert close and "stop" in why


def test_dte_floor_closes_even_when_flat():
    close, why = mgmt.evaluate_exit(pnl_pct=2.0, dte=5)
    assert close and "DTE" in why


def test_within_bounds_holds():
    close, why = mgmt.evaluate_exit(pnl_pct=10.0, dte=30)
    assert not close and why.startswith("hold")


# --- evaluate_exit: the D60 time stop ---------------------------------------


def test_time_stop_closes_past_n_days_when_no_other_rule_hit():
    close, why = mgmt.evaluate_exit(pnl_pct=-12.0, dte=30, held_days=2.4)
    assert close and "time stop" in why


def test_time_stop_does_not_fire_before_n_days():
    close, why = mgmt.evaluate_exit(pnl_pct=-12.0, dte=30, held_days=1.9)
    assert not close and why.startswith("hold")


def test_time_stop_does_not_fire_when_age_unknown():
    close, why = mgmt.evaluate_exit(pnl_pct=-12.0, dte=30, held_days=None)
    assert not close and why.startswith("hold")


def test_profit_target_takes_precedence_over_time_stop():
    close, why = mgmt.evaluate_exit(pnl_pct=75.0, dte=30, held_days=9.0)
    assert close and "profit target" in why and "time stop" not in why


def test_stop_loss_takes_precedence_over_time_stop():
    close, why = mgmt.evaluate_exit(pnl_pct=-60.0, dte=30, held_days=9.0)
    assert close and "stop" in why and "time stop" not in why


def test_dte_floor_takes_precedence_over_time_stop():
    close, why = mgmt.evaluate_exit(pnl_pct=2.0, dte=5, held_days=9.0)
    assert close and "DTE" in why and "time stop" not in why


# --- run_management_sweep ----------------------------------------------


def _seed_option_buy(conn, occ="AAPL261016C00200000"):
    sid = db.insert_strategy(conn, name="S", symbol="AAPL", schema_json="{}",
                             rationale=None, source="llm", dedup_key=occ)
    run_id = db.start_run(conn)
    db.insert_order(conn, strategy_id=sid, run_id=run_id, symbol=occ, qty=1.0,
                    side="buy", status="filled", broker_order_id="b1",
                    asset_class="option", contract_symbol=occ, underlying="AAPL",
                    premium=300.0)
    return sid, run_id


def _pos(occ="AAPL261016C00200000", plpc=0.8, qty="1"):
    return {"symbol": occ, "asset_class": "us_option", "qty": qty,
            "avg_entry_price": "3.00", "current_price": "5.40",
            "unrealized_plpc": str(plpc)}


def test_sweep_closes_a_winner_and_records_the_sell(conn, monkeypatch):
    _seed_option_buy(conn)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions", lambda: [_pos(plpc=0.8)])
    monkeypatch.setattr(mgmt.mcp_client, "close_position",
                        lambda occ, **k: mgmt.mcp_client.OrderResult(
                            ok=True, status="accepted", broker_order_id="c1",
                            symbol=occ, side="sell", raw='{"id":"c1"}'))
    res = mgmt.run_management_sweep(conn)
    assert res.evaluated == 1 and res.closed == 1 and res.held == 0

    orders = db.list_orders(conn)
    sell = [o for o in orders if o["side"] == "sell"]
    assert len(sell) == 1
    assert sell[0]["asset_class"] == "option"
    assert sell[0]["broker_order_id"] == "c1"
    assert sell[0]["selection_reason"].startswith("EXIT —")
    # the original buy is flipped so the risk caps stop counting it
    buy = [o for o in orders if o["side"] == "buy"][0]
    assert buy["status"] == "closed"


def test_sweep_holds_a_position_within_bounds(conn, monkeypatch):
    _seed_option_buy(conn)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions", lambda: [_pos(plpc=0.1)])
    called = {"n": 0}
    monkeypatch.setattr(mgmt.mcp_client, "close_position",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    res = mgmt.run_management_sweep(conn)
    assert res.held == 1 and res.closed == 0
    assert called["n"] == 0
    assert [o for o in db.list_orders(conn) if o["side"] == "sell"] == []


def test_sweep_dry_run_does_not_call_broker(conn, monkeypatch):
    _seed_option_buy(conn)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions", lambda: [_pos(plpc=0.9)])
    called = {"n": 0}
    monkeypatch.setattr(mgmt.mcp_client, "close_position",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    res = mgmt.run_management_sweep(conn, dry_run=True)
    assert res.closed == 1 and called["n"] == 0
    sell = [o for o in db.list_orders(conn) if o["side"] == "sell"][0]
    assert sell["status"] == "dry_run" and sell["dry_run"] == 1


def test_sweep_ignores_equity_positions(conn, monkeypatch):
    monkeypatch.setattr(mgmt.mcp_client, "list_positions",
                        lambda: [{"symbol": "AAPL", "asset_class": "us_equity",
                                  "qty": "10", "unrealized_plpc": "0.9"}])
    res = mgmt.run_management_sweep(conn)
    assert res.evaluated == 0


def test_sweep_survives_a_broker_read_failure(conn, monkeypatch):
    def boom():
        raise RuntimeError("mcp down")
    monkeypatch.setattr(mgmt.mcp_client, "list_positions", boom)
    res = mgmt.run_management_sweep(conn)
    assert res.error == "mcp down"
    assert res.evaluated == 0


# --- run_management_sweep: the D60 time stop -------------------------------

_OCC = "AAPL261016C00200000"


def _closes_ok(occ, **k):
    return mgmt.mcp_client.OrderResult(ok=True, status="accepted", broker_order_id="c1",
                                       symbol=occ, side="sell", raw='{"id":"c1"}')


def test_sweep_time_stop_closes_a_stale_position(conn, monkeypatch):
    _seed_option_buy(conn, occ=_OCC)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions",
                        lambda: [_pos(occ=_OCC, plpc=-0.15)])  # within +60/-50
    monkeypatch.setattr(mgmt.mcp_client, "list_recent_orders",
                        lambda limit=100: [{"symbol": _OCC, "side": "buy",
                                            "status": "filled",
                                            "filled_at": _iso_days_ago(3.0)}])
    monkeypatch.setattr(mgmt.mcp_client, "close_position", _closes_ok)
    res = mgmt.run_management_sweep(conn)
    assert res.closed == 1 and res.held == 0
    sell = [o for o in db.list_orders(conn) if o["side"] == "sell"][0]
    assert "time stop" in sell["selection_reason"]


def test_sweep_time_stop_holds_a_fresh_position(conn, monkeypatch):
    _seed_option_buy(conn, occ=_OCC)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions",
                        lambda: [_pos(occ=_OCC, plpc=-0.15)])
    monkeypatch.setattr(mgmt.mcp_client, "list_recent_orders",
                        lambda limit=100: [{"symbol": _OCC, "side": "buy",
                                            "status": "filled",
                                            "filled_at": _iso_days_ago(0.5)}])
    monkeypatch.setattr(mgmt.mcp_client, "close_position", _closes_ok)
    res = mgmt.run_management_sweep(conn)
    assert res.held == 1 and res.closed == 0


def test_sweep_time_stop_folds_multiple_fills_to_earliest(conn, monkeypatch):
    _seed_option_buy(conn, occ=_OCC)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions",
                        lambda: [_pos(occ=_OCC, plpc=-0.15, qty="2")])
    monkeypatch.setattr(mgmt.mcp_client, "list_recent_orders",
                        lambda limit=100: [
                            {"symbol": _OCC, "side": "buy", "status": "filled",
                             "filled_at": _iso_days_ago(0.1)},
                            {"symbol": _OCC, "side": "buy", "status": "filled",
                             "filled_at": _iso_days_ago(3.0)},
                        ])
    monkeypatch.setattr(mgmt.mcp_client, "close_position", _closes_ok)
    res = mgmt.run_management_sweep(conn)
    assert res.closed == 1


def test_sweep_time_stop_skipped_when_no_broker_fill_time(conn, monkeypatch):
    _seed_option_buy(conn, occ=_OCC)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions",
                        lambda: [_pos(occ=_OCC, plpc=-0.15)])
    # order history has no filled BUY for this contract
    monkeypatch.setattr(mgmt.mcp_client, "list_recent_orders", lambda limit=100: [])
    called = {"n": 0}
    monkeypatch.setattr(mgmt.mcp_client, "close_position",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    res = mgmt.run_management_sweep(conn)
    assert res.held == 1 and res.closed == 0 and called["n"] == 0


def test_sweep_survives_an_order_history_read_failure(conn, monkeypatch):
    _seed_option_buy(conn, occ=_OCC)
    monkeypatch.setattr(mgmt.mcp_client, "list_positions",
                        lambda: [_pos(occ=_OCC, plpc=-0.15)])

    def boom(limit=100):
        raise RuntimeError("orders endpoint down")
    monkeypatch.setattr(mgmt.mcp_client, "list_recent_orders", boom)
    monkeypatch.setattr(mgmt.mcp_client, "close_position", _closes_ok)
    res = mgmt.run_management_sweep(conn)
    # time stop disabled this pass; other rules still evaluated, position held
    assert res.error is None and res.held == 1 and res.closed == 0
