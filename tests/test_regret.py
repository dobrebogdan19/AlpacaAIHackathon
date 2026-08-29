"""regret.py — the regret-ledger orchestration (Phase 4).

The engine and the gate have their own tests; here the engine is faked so the
test controls in-sample vs forward returns directly and checks the wiring:
both backtest rows stored, as-of decisions recorded, retirement applied,
position closed through the injected close_fn, post-mortem persisted, and the
selection-bias number computed from the records.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

import db
import regret
from schema import Condition, IndicatorName, IndicatorRef, Operator, Rule, Strategy


def _strategy(name, symbol):
    ref = lambda p: IndicatorRef(indicator=IndicatorName.SMA, period=p)
    return Strategy(
        name=name, symbol=symbol, rationale="x",
        entry=Rule(conditions=[Condition(left=ref(3), operator=Operator.CROSSES_ABOVE, right=ref(7))]),
        exit=Rule(conditions=[Condition(left=ref(3), operator=Operator.CROSSES_BELOW, right=ref(7))]),
    )


def _bars(n=200):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"timestamp": base + timedelta(days=i), "open": 100.0 + i,
             "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i,
             "volume": 1e6} for i in range(n)]


@dataclass
class _FakeClose:
    status: str = "accepted"
    qty: float | None = 3.0
    broker_order_id: str | None = "close-1"
    raw: str = "{}"
    error: str | None = None


# in-sample / forward total return per strategy name
_RETS = {
    ("winner", "in"): 15.0, ("winner", "fwd"): -8.0,   # promoted in-sample, loses forward
    ("loser", "in"): -5.0, ("loser", "fwd"): 6.0,       # rejected in-sample, wins forward
}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "regret.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(regret.data, "get_bars", lambda *a, **k: _bars())

    def fake_bt(strategy, bars, *, start_index=0):
        key = (strategy.name, "fwd" if start_index else "in")
        return {
            "total_return_pct": _RETS[key], "max_drawdown_pct": 5.0,
            "num_trades": 12, "win_rate_pct": 50.0, "open_position": False,
            "unrealized_pnl_pct": 0.0,
            "equity_curve": [{"date": "2026-01-01", "equity": 10000},
                             {"date": "2026-06-01", "equity": 10000 * (1 + _RETS[key] / 100)}],
        }

    monkeypatch.setattr(regret.engine, "run_backtest", fake_bt)


def _seed_two(conn):
    w = db.insert_strategy(conn, name="winner", symbol="AAPL",
                           schema_json=_strategy("winner", "AAPL").model_dump_json(),
                           rationale="x", source="llm", status="active", dedup_key="w")
    l = db.insert_strategy(conn, name="loser", symbol="AAPL",
                           schema_json=_strategy("loser", "AAPL").model_dump_json(),
                           rationale="x", source="llm", status="rejected", dedup_key="l")
    return w, l


def test_stores_both_backtest_halves_and_asof_decisions(conn):
    _seed_two(conn)
    res = regret.run_regret_ledger(as_of="2026-05-01", conn=conn, dry_run=True,
                                   close_fn=lambda *a, **k: _FakeClose(),
                                   postmortem_fn=lambda facts: "PM")

    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM backtests WHERE run_id = ?", (res.run_id,))]
    assert sorted(kinds) == ["forward", "forward", "insample", "insample"]
    assert all(r["as_of"] == "2026-05-01" for r in conn.execute(
        "SELECT as_of FROM backtests WHERE run_id = ?", (res.run_id,)))

    outcomes = {d["outcome"] for d in conn.execute(
        "SELECT outcome FROM decisions WHERE run_id = ? AND outcome IN ('promoted','rejected')",
        (res.run_id,))}
    assert outcomes == {"promoted", "rejected"}
    run = db.get_run(conn, res.run_id)
    assert run["as_of"] == "2026-05-01"


def test_retirement_fires_closes_position_and_writes_postmortem(conn):
    w, l = _seed_two(conn)
    # the retired (active) strategy is holding a live paper position
    db.insert_order(conn, strategy_id=w, run_id=None, symbol="AAPL", qty=3.0,
                    side="buy", status="accepted", broker_order_id="buy-1")

    closed = {}
    def fake_close(symbol, *, dry_run=None):
        closed["symbol"] = symbol
        return _FakeClose(status="accepted")

    res = regret.run_regret_ledger(as_of="2026-05-01", conn=conn, dry_run=True,
                                   close_fn=fake_close, postmortem_fn=lambda facts: "PM TEXT")

    assert len(res.retirements) == 1
    assert db.get_strategy(conn, w)["status"] == "retired"
    assert db.get_strategy(conn, l)["status"] == "active"
    assert closed["symbol"] == "AAPL"

    sells = [o for o in db.list_orders(conn) if o["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["submitted_via"] == "mcp"

    pms = db.list_postmortems(conn)
    assert len(pms) == 1
    assert pms[0]["text"] == "PM TEXT"
    assert pms[0]["retired_strategy_id"] == w and pms[0]["promoted_strategy_id"] == l
    retired_decision = conn.execute(
        "SELECT * FROM decisions WHERE run_id = ? AND outcome = 'retired'", (res.run_id,)
    ).fetchone()
    assert retired_decision is not None


def test_selection_bias_is_computed_from_records(conn):
    _seed_two(conn)
    res = regret.run_regret_ledger(as_of="2026-05-01", conn=conn, dry_run=True,
                                   close_fn=lambda *a, **k: _FakeClose(),
                                   postmortem_fn=lambda facts: "PM")
    sb = res.selection_bias
    assert sb.n_promoted == 1 and sb.n_rejected == 1
    assert sb.promoted_avg_forward_return_pct == -8.0
    assert sb.rejected_avg_forward_return_pct == 6.0
    assert sb.spread_pp == -14.0


def test_no_retirement_leaves_statuses_untouched(conn):
    w, l = _seed_two(conn)
    # flip the forward returns so the shadow does NOT beat the active
    monkey = dict(_RETS)
    _RETS[("loser", "fwd")] = -20.0
    try:
        res = regret.run_regret_ledger(as_of="2026-05-01", conn=conn, dry_run=True,
                                       close_fn=lambda *a, **k: _FakeClose(),
                                       postmortem_fn=lambda facts: "PM")
        assert res.retirements == []
        assert db.get_strategy(conn, w)["status"] == "active"
        assert db.get_strategy(conn, l)["status"] == "rejected"
    finally:
        _RETS.update(monkey)
