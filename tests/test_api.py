"""API surface tests — all offline, against a temp DB with hand-inserted rows.

Nothing here touches the network, an LLM, or the MCP subprocess: the read
endpoints must render from stored rows alone (D6), and ``POST /api/cycle`` is
tested with ``cycle.run_cycle`` stubbed.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "t.db"
    monkeypatch.delenv("DB_PATH", raising=False)

    import db
    monkeypatch.setattr(db, "DB_PATH", db_file)

    import api  # _bootstrap_db() is a no-op without DB_PATH set

    conn = db.connect(db_file)
    _seed(conn)
    conn.close()

    return TestClient(api.app)


def _seed(conn):
    import db
    # one promoting run
    r1 = db.start_run(conn)
    s1 = db.insert_strategy(conn, name="Fast SPY", symbol="SPY",
                            schema_json=json.dumps({"symbol": "SPY"}), rationale="r",
                            source="manual", status="active", dedup_key="k1")
    db.insert_backtest(conn, strategy_id=s1, run_id=r1,
                       metrics_json=json.dumps({"total_return_pct": 12.0, "max_drawdown_pct": 8.0,
                                                "num_trades": 14, "win_rate_pct": 57.0,
                                                "open_position": False}),
                       equity_curve_json=json.dumps([{"date": "2025-01-01", "equity": 10000},
                                                     {"date": "2025-06-01", "equity": 11200}]),
                       bars_start="2025-01-01", bars_end="2025-06-01")
    db.insert_decision(conn, strategy_id=s1, run_id=r1, outcome="promoted", reason="all thresholds passed")
    db.insert_order(conn, strategy_id=s1, run_id=r1, symbol="SPY", qty=3, side="buy",
                    status="accepted", broker_order_id="brk-1", submitted_via="mcp")
    db.finish_run(conn, r1, n_generated=1, n_promoted=1, n_rejected=0)

    # one all-rejected run
    r2 = db.start_run(conn)
    s2 = db.insert_strategy(conn, name="Slow AAPL", symbol="AAPL",
                            schema_json=json.dumps({"symbol": "AAPL"}), rationale="r",
                            source="llm", status="rejected", dedup_key="k2",
                            raw_llm_output='{"strategies": []}')
    db.insert_backtest(conn, strategy_id=s2, run_id=r2,
                       metrics_json=json.dumps({"total_return_pct": 3.0, "max_drawdown_pct": 5.0,
                                                "num_trades": 2, "win_rate_pct": 50.0,
                                                "open_position": False}),
                       equity_curve_json=json.dumps([{"date": "2025-01-01", "equity": 10000}]),
                       bars_start="2025-01-01", bars_end="2025-06-01")
    db.insert_decision(conn, strategy_id=s2, run_id=r2, outcome="rejected",
                       reason="only 2 realised trade(s), 10 required")
    db.finish_run(conn, r2, n_generated=1, n_promoted=0, n_rejected=1)

    # a regret-ledger (as-of) run: s1 promoted-as-of but loses forward, s2
    # rejected-as-of but wins forward -> a retirement of s1 in favour of s2.
    r3 = db.start_run(conn)
    db.set_run_as_of(conn, r3, "2026-06-15")
    for sid, dec, fwd in ((s1, "promoted", -6.0), (s2, "rejected", 5.5)):
        db.insert_backtest(conn, strategy_id=sid, run_id=r3,
                           metrics_json=json.dumps({"total_return_pct": 8.0, "max_drawdown_pct": 5.0,
                                                    "num_trades": 12, "win_rate_pct": 50.0}),
                           equity_curve_json=json.dumps([{"date": "2026-02-01", "equity": 10000}]),
                           bars_start="2026-02-01", bars_end="2026-06-15",
                           kind="insample", as_of="2026-06-15")
        db.insert_backtest(conn, strategy_id=sid, run_id=r3,
                           metrics_json=json.dumps({"total_return_pct": fwd, "max_drawdown_pct": 5.0,
                                                    "num_trades": 4, "win_rate_pct": 50.0,
                                                    "forward_bars": 50}),
                           equity_curve_json=json.dumps([{"date": "2026-06-16", "equity": 10000},
                                                         {"date": "2026-08-20", "equity": 10000 * (1 + fwd / 100)}]),
                           bars_start="2026-06-16", bars_end="2026-08-20",
                           kind="forward", as_of="2026-06-15")
        db.insert_decision(conn, strategy_id=sid, run_id=r3, outcome=dec,
                           reason=f"as-of 2026-06-15 gate: {dec}")
    d_ret = db.insert_decision(conn, strategy_id=s1, run_id=r3, outcome="retired",
                               reason="shadow 'Slow AAPL' returned +5.50% vs active 'Fast SPY' -6.00%")
    db.insert_postmortem(conn, decision_id=d_ret, run_id=r3, retired_strategy_id=s1,
                         promoted_strategy_id=s2, facts_json=json.dumps({"symbol": "SPY"}),
                         text="The retired strategy looked fine in-sample but lost forward.")
    db.finish_run(conn, r3, n_generated=2, n_promoted=1, n_rejected=1)

    db.insert_calibration(conn, run_id=r3, as_of="2026-06-15",
                          record_json=json.dumps({"verdict": "does-not-survive-holdout",
                                                  "applied": False, "run_id": r3}))


def test_health_reports_counts_and_no_network(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok"
    assert h["counts"] == {"runs": 3, "strategies": 2, "orders": 1}
    assert h["kill_switch"] is False


def test_runs_list_newest_first_with_counts(client):
    runs = client.get("/api/runs").json()["runs"]
    assert [r["id"] for r in runs] == [3, 2, 1]
    assert runs[2]["n_decisions"] == 1 and runs[2]["n_orders"] == 1


def test_run_detail_has_every_candidate_with_reason(client):
    d = client.get("/api/runs/1").json()
    assert d["run"]["n_promoted"] == 1
    assert len(d["candidates"]) == 1
    c = d["candidates"][0]
    assert c["outcome"] == "promoted" and c["reason"]
    assert c["metrics"]["num_trades"] == 14
    assert d["orders"][0]["submitted_via"] == "mcp"


def test_run_detail_404(client):
    assert client.get("/api/runs/999").status_code == 404


def test_strategies_list_and_detail(client):
    lst = client.get("/api/strategies").json()["strategies"]
    assert {s["status"] for s in lst} == {"active", "rejected"}
    assert lst[0]["latest_metrics"]["total_return_pct"] == 12.0

    detail = client.get("/api/strategies/2").json()
    assert detail["strategy"]["raw_llm_output"] == '{"strategies": []}'
    assert detail["backtests"][0]["equity_curve"][0]["equity"] == 10000
    assert detail["decisions"][0]["outcome"] == "rejected"


def test_orders_endpoint_shows_mcp(client):
    orders = client.get("/api/orders").json()["orders"]
    assert orders[0]["submitted_via"] == "mcp"
    assert orders[0]["strategy_name"] == "Fast SPY"


def test_equity_curves_only_promoted(client):
    series = client.get("/api/equity-curves").json()["series"]
    assert len(series) == 1
    assert series[0]["symbol"] == "SPY"
    assert series[0]["points"][-1]["equity"] == 11200


def test_shadow_curves_tags_decision_and_status(client):
    body = client.get("/api/shadow-curves").json()
    assert body["as_of"] == "2026-06-15"
    assert body["simulation"] is True
    assert len(body["series"]) == 2
    by_name = {s["name"]: s for s in body["series"]}
    assert by_name["Fast SPY"]["as_of_decision"] == "promoted"
    assert by_name["Slow AAPL"]["as_of_decision"] == "rejected"
    assert by_name["Fast SPY"]["tracking_start"] == "2026-06-16"
    assert by_name["Slow AAPL"]["points"][-1]["equity"] == pytest.approx(10550.0)


def test_selection_bias_number_and_sample_size(client):
    sb = client.get("/api/selection-bias").json()
    assert sb["available"] is True
    assert sb["as_of"] == "2026-06-15"
    assert sb["promoted_avg_forward_return_pct"] == -6.0
    assert sb["rejected_avg_forward_return_pct"] == 5.5
    assert sb["n_promoted"] == 1 and sb["n_rejected"] == 1
    assert sb["spread_pp"] == -11.5


def test_retirements_endpoint_carries_postmortem(client):
    rets = client.get("/api/retirements").json()["retirements"]
    assert len(rets) == 1
    assert rets[0]["retired_name"] == "Fast SPY"
    assert rets[0]["promoted_name"] == "Slow AAPL"
    assert "lost forward" in rets[0]["text"]
    assert rets[0]["facts"]["symbol"] == "SPY"


def test_scheduler_endpoint_reads_ticks_and_config(client, monkeypatch):
    import db
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    conn = db.connect()
    db.insert_scheduler_tick(conn, market_open=False, action="skipped-market-closed",
                             detail="next open soon")
    db.insert_scheduler_tick(conn, market_open=True, action="manage-only", detail="0 held")
    conn.close()

    body = client.get("/api/scheduler").json()
    assert body["config"]["enabled"] is False
    assert body["tick_counts"]["skipped-market-closed"] == 1
    assert body["ticks"][0]["action"] == "manage-only"        # DESC


def test_calibration_endpoint_returns_latest_record(client):
    body = client.get("/api/calibration").json()
    assert body["available"] is True
    assert body["applied"] is False
    assert body["record"]["verdict"] == "does-not-survive-holdout"


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Decision log" in r.text


def test_cycle_triggers_background_run_and_rate_limits(client, monkeypatch):
    import cycle

    calls = []

    def fake_run_cycle(**kw):
        calls.append(kw)
        conn = __import__("db").connect()
        __import__("db").finish_run(conn, kw["run_id"], n_generated=2, n_promoted=1, n_rejected=1)
        conn.close()
        class _R:  # noqa
            def summary(self): return "run done"
        return _R()

    monkeypatch.setattr(cycle, "run_cycle", fake_run_cycle)

    first = client.post("/api/cycle")
    assert first.status_code == 202
    rid = first.json()["run_id"]
    assert calls and calls[0]["run_id"] == rid

    second = client.post("/api/cycle")
    assert second.status_code == 429
