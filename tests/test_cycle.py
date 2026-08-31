"""cycle.py tests (the end-to-end runner).

Acceptance focus for Phase 3: EVERY candidate ends with a stored decision reason
(none silently dropped), and every promotion produces a persisted order row
(dry_run / blocked / a status). Generation, bar-fetch and the MCP call are
faked — their own modules test the real thing.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

import cycle
import db
from schema import Condition, IndicatorName, IndicatorRef, Operator, Rule, Strategy


# --- fakes ---------------------------------------------------------------


def _strategy(name, symbol="AAPL", fast=10, slow=30):
    return Strategy(
        name=name, symbol=symbol, rationale="x",
        entry=Rule(conditions=[Condition(
            left=IndicatorRef(indicator=IndicatorName.SMA, period=fast),
            operator=Operator.CROSSES_ABOVE,
            right=IndicatorRef(indicator=IndicatorName.SMA, period=slow))], join=None),
        exit=Rule(conditions=[Condition(
            left=IndicatorRef(indicator=IndicatorName.SMA, period=fast),
            operator=Operator.CROSSES_BELOW,
            right=IndicatorRef(indicator=IndicatorName.SMA, period=slow))], join=None),
    )


@dataclass
class _Gen:
    strategies: list
    strategy_ids: list


def _fake_generate_factory(strategies):
    def _gen(*, n, symbols, conn, run_id):
        ids = []
        for s in strategies:
            sid = db.insert_strategy(
                conn, name=s.name, symbol=s.symbol, schema_json=s.model_dump_json(),
                rationale=s.rationale, source="llm", status="candidate",
                dedup_key=s.name,
            )
            ids.append(sid)
        return _Gen(list(strategies), ids)
    return _gen


def _fake_bars(n=60):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {"timestamp": base + timedelta(days=i), "open": 100.0 + i, "high": 101.0 + i,
         "low": 99.0 + i, "close": 100.5 + i, "volume": 1_000_000.0}
        for i in range(n)
    ]


PROMOTE = {"total_return_pct": 8.0, "max_drawdown_pct": 5.0, "num_trades": 15,
           "win_rate_pct": 60.0, "open_position": False, "unrealized_pnl_pct": 0.0,
           "equity_curve": [{"date": "2026-01-01", "equity": 10000}]}
REJECT = {"total_return_pct": 1.0, "max_drawdown_pct": 5.0, "num_trades": 2,
          "win_rate_pct": 50.0, "open_position": False, "unrealized_pnl_pct": 0.0,
          "equity_curve": [{"date": "2026-01-01", "equity": 10000}]}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "cycle.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    # The default expression is options (D48); these Phase 3 tests cover the
    # equity path, which is still a supported one-line toggle. The option path
    # has its own offline tests below.
    monkeypatch.setattr(cycle, "EXPRESSION", "equity")
    monkeypatch.setattr(cycle.data, "get_bars", lambda *a, **k: _fake_bars())
    # strategy 0 promotes, everyone else rejects
    seq = {"i": 0}

    def fake_bt(strategy, bars):
        m = PROMOTE if seq["i"] == 0 else REJECT
        seq["i"] += 1
        return dict(m)

    monkeypatch.setattr(cycle.engine, "run_backtest", fake_bt)


# --- tests -------------------------------------------------------------------


def test_every_candidate_gets_a_decision_row(conn, monkeypatch):
    monkeypatch.setattr(cycle.mcp_client, "submit_market_order",
                        lambda *a, **k: cycle.mcp_client.OrderResult(
                            ok=True, status="dry_run", symbol="AAPL", side="buy"))
    strategies = [_strategy(f"s{i}", slow=20 + i) for i in range(4)]
    res = cycle.run_cycle(conn=conn, n=4, dry_run=True,
                          generate_fn=_fake_generate_factory(strategies))

    decisions = db.list_decisions(conn)
    assert len(decisions) == 4                    # nobody dropped
    assert all(d["reason"].strip() for d in decisions)
    assert res.n_promoted == 1 and res.n_rejected == 3
    assert {d["outcome"] for d in decisions} == {"promoted", "rejected"}


def test_promoted_strategy_produces_an_order_row(conn, monkeypatch):
    captured = {}
    monkeypatch.setattr(cycle.mcp_client, "submit_market_order",
                        lambda *a, **k: cycle.mcp_client.OrderResult(
                            ok=True, status="accepted", broker_order_id="ord-9",
                            symbol="AAPL", side="buy", raw='{"id":"ord-9"}'))
    strategies = [_strategy("winner"), _strategy("loser", slow=40)]
    res = cycle.run_cycle(conn=conn, n=2, dry_run=False,
                          generate_fn=_fake_generate_factory(strategies))

    orders = db.list_orders(conn)
    assert len(orders) == 1
    assert orders[0]["broker_order_id"] == "ord-9"
    assert orders[0]["submitted_via"] == "mcp"
    assert orders[0]["status"] == "accepted"
    winner = db.list_strategies(conn, status="active")
    assert len(winner) == 1 and winner[0]["name"] == "winner"


def test_kill_switch_blocks_the_order_but_still_records_it(conn, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "1")
    called = {"n": 0}
    monkeypatch.setattr(cycle.mcp_client, "submit_market_order",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    strategies = [_strategy("winner")]
    res = cycle.run_cycle(conn=conn, n=1, dry_run=False,
                          generate_fn=_fake_generate_factory(strategies))

    assert called["n"] == 0                       # order path never reached
    orders = db.list_orders(conn)
    assert len(orders) == 1
    assert orders[0]["status"] == "blocked"
    assert "kill switch" in orders[0]["raw_response"].lower()
    assert res.n_orders_blocked == 1 and res.n_orders_submitted == 0


def test_dry_run_records_a_dry_run_order(conn, monkeypatch):
    monkeypatch.setattr(cycle.mcp_client, "submit_market_order",
                        cycle.mcp_client.submit_market_order)  # real fn, dry path only
    strategies = [_strategy("winner")]
    res = cycle.run_cycle(conn=conn, n=1, dry_run=True,
                          generate_fn=_fake_generate_factory(strategies))
    orders = db.list_orders(conn)
    assert len(orders) == 1
    assert orders[0]["status"] == "dry_run"
    assert orders[0]["dry_run"] == 1
    assert res.n_orders_submitted == 0


def test_run_row_is_finished_with_counts(conn, monkeypatch):
    monkeypatch.setattr(cycle.mcp_client, "submit_market_order",
                        lambda *a, **k: cycle.mcp_client.OrderResult(
                            ok=True, status="dry_run", symbol="AAPL", side="buy"))
    strategies = [_strategy(f"s{i}", slow=20 + i) for i in range(3)]
    res = cycle.run_cycle(conn=conn, n=3, dry_run=True,
                          generate_fn=_fake_generate_factory(strategies))
    row = db.get_run(conn, res.run_id)
    assert row["finished_at"]
    assert (row["n_generated"], row["n_promoted"], row["n_rejected"]) == (3, 1, 2)


# --- option expression path (D48) — offline: chain + MCP order faked -------


@pytest.fixture
def _options_expr(monkeypatch):
    monkeypatch.setattr(cycle, "EXPRESSION", "options")


def _choice(occ="AAPL260925C00165000"):
    return cycle.options.ContractChoice(
        occ_symbol=occ, underlying="AAPL", right="call", strike=165.0,
        expiry="2026-09-25", dte=38, spot=159.5, bid=3.10, ask=3.40,
        premium=340.0, reason="long call AAPL $165 exp 2026-09-25 (38 DTE): closest to target",
    )


def test_promoted_strategy_places_an_option_order(conn, monkeypatch, _options_expr):
    monkeypatch.setattr(cycle.options, "select_contract", lambda *a, **k: _choice())
    monkeypatch.setattr(cycle.mcp_client, "submit_option_order",
                        lambda *a, **k: cycle.mcp_client.OrderResult(
                            ok=True, status="accepted", broker_order_id="opt-1",
                            symbol="AAPL260925C00165000", qty=1.0, side="buy",
                            raw='{"id":"opt-1"}'))
    strategies = [_strategy("winner"), _strategy("loser", slow=40)]
    res = cycle.run_cycle(conn=conn, n=2, dry_run=False,
                          generate_fn=_fake_generate_factory(strategies))

    orders = db.list_orders(conn)
    assert len(orders) == 1
    o = orders[0]
    assert o["asset_class"] == "option"
    assert o["contract_symbol"] == "AAPL260925C00165000"
    assert o["underlying"] == "AAPL"
    assert o["strike"] == 165.0
    assert o["expiry"] == "2026-09-25"
    assert o["premium"] == 340.0
    assert o["selection_reason"].startswith("long call AAPL")
    assert o["broker_order_id"] == "opt-1"
    assert res.n_orders_submitted == 1


def test_no_suitable_contract_is_recorded_as_skipped_not_crash(conn, monkeypatch, _options_expr):
    monkeypatch.setattr(cycle.options, "select_contract",
                        lambda *a, **k: cycle.options.NoContract("AAPL", "no call cleared the rules"))
    called = {"n": 0}
    monkeypatch.setattr(cycle.mcp_client, "submit_option_order",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    strategies = [_strategy("winner")]
    res = cycle.run_cycle(conn=conn, n=1, dry_run=False,
                          generate_fn=_fake_generate_factory(strategies))

    assert called["n"] == 0
    orders = db.list_orders(conn)
    assert len(orders) == 1
    assert orders[0]["status"] == "skipped"
    assert orders[0]["asset_class"] == "option"
    assert "no call cleared" in orders[0]["selection_reason"]
    assert res.n_promoted == 1
    assert res.n_orders_skipped == 1 and res.n_orders_submitted == 0


def test_option_premium_cap_blocks_and_records(conn, monkeypatch, _options_expr):
    monkeypatch.setattr(cycle.options, "select_contract",
                        lambda *a, **k: _choice())
    monkeypatch.setattr(cycle.risk, "MAX_TOTAL_OPTION_PREMIUM_AT_RISK", 100.0)
    called = {"n": 0}
    monkeypatch.setattr(cycle.mcp_client, "submit_option_order",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    strategies = [_strategy("winner")]
    res = cycle.run_cycle(conn=conn, n=1, dry_run=False,
                          generate_fn=_fake_generate_factory(strategies))

    assert called["n"] == 0
    orders = db.list_orders(conn)
    assert len(orders) == 1
    assert orders[0]["status"] == "blocked"
    assert orders[0]["asset_class"] == "option"
    assert orders[0]["contract_symbol"] == "AAPL260925C00165000"
    assert "premium at risk" in orders[0]["raw_response"]
    assert res.n_orders_blocked == 1


def test_option_order_dry_run_records_dry_run(conn, monkeypatch, _options_expr):
    monkeypatch.setattr(cycle.options, "select_contract", lambda *a, **k: _choice())
    monkeypatch.setattr(cycle.mcp_client, "submit_option_order",
                        cycle.mcp_client.submit_option_order)  # real fn, dry path only
    strategies = [_strategy("winner")]
    res = cycle.run_cycle(conn=conn, n=1, dry_run=True,
                          generate_fn=_fake_generate_factory(strategies))
    orders = db.list_orders(conn)
    assert len(orders) == 1
    assert orders[0]["status"] == "dry_run"
    assert orders[0]["dry_run"] == 1
    assert orders[0]["asset_class"] == "option"
    assert res.n_orders_submitted == 0
