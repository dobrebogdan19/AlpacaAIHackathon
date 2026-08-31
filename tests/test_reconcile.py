"""reconcile.py tests — startup sync of the orders table against the broker.

A local row the broker cannot confirm is marked ``reconciled-closed`` (terminal,
so ``risk`` stops counting it); a confirmed one is left alone; a broker read
failure changes nothing.
"""

import pytest

import db
import reconcile
import risk


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "rec.db")
    yield c
    c.close()


def _order(conn, name, symbol, *, status="accepted", asset_class="equity",
           contract_symbol=None):
    sid = db.insert_strategy(conn, name=name, symbol=symbol, schema_json="{}",
                             rationale=None, source="llm", dedup_key=name)
    db.insert_order(conn, strategy_id=sid, run_id=None, symbol=symbol, qty=1.0,
                    side="buy", status=status, broker_order_id=f"o-{name}",
                    asset_class=asset_class, contract_symbol=contract_symbol,
                    underlying=symbol if asset_class == "option" else None)


def test_stale_row_is_marked_reconciled_closed(conn, monkeypatch):
    _order(conn, "gone", "AAPL")
    _order(conn, "held", "MSFT")
    monkeypatch.setattr(reconcile.mcp_client, "list_positions",
                        lambda: [{"symbol": "MSFT", "qty": "3"}])
    monkeypatch.setattr(reconcile.mcp_client, "list_open_orders", lambda: [])

    summary = reconcile.reconcile_orders(conn)
    assert summary["checked"] == 2 and summary["closed"] == 1 and summary["kept"] == 1

    rows = {o["broker_order_id"]: o["status"] for o in db.list_orders(conn)}
    assert rows["o-gone"] == "reconciled-closed"
    assert rows["o-held"] == "accepted"
    # and risk now treats the closed one as terminal
    assert "reconciled-closed" in risk._TERMINAL_ORDER_STATUSES


def test_open_order_at_broker_keeps_the_row(conn, monkeypatch):
    _order(conn, "working", "SPY")
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", lambda: [])
    monkeypatch.setattr(reconcile.mcp_client, "list_open_orders",
                        lambda: [{"symbol": "SPY", "status": "new"}])
    summary = reconcile.reconcile_orders(conn)
    assert summary["closed"] == 0 and summary["kept"] == 1


def test_option_row_matched_by_contract_symbol(conn, monkeypatch):
    occ = "AAPL260116C00200000"
    _order(conn, "opt", "AAPL", asset_class="option", contract_symbol=occ)
    monkeypatch.setattr(reconcile.mcp_client, "list_positions",
                        lambda: [{"symbol": occ, "qty": "1"}])
    monkeypatch.setattr(reconcile.mcp_client, "list_open_orders", lambda: [])
    summary = reconcile.reconcile_orders(conn)
    assert summary["closed"] == 0


def test_broker_read_failure_changes_nothing(conn, monkeypatch):
    _order(conn, "x", "AAPL")

    def boom():
        raise RuntimeError("mcp unreachable")
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", boom)
    monkeypatch.setattr(reconcile.mcp_client, "list_open_orders", boom)

    summary = reconcile.reconcile_orders(conn)
    assert summary["skipped"] and summary["closed"] == 0
    assert db.list_orders(conn)[0]["status"] == "accepted"


def test_already_terminal_rows_are_ignored(conn, monkeypatch):
    _order(conn, "canceled", "AAPL", status="canceled")
    _order(conn, "skipped", "MSFT", status="skipped")
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", lambda: [])
    monkeypatch.setattr(reconcile.mcp_client, "list_open_orders", lambda: [])
    summary = reconcile.reconcile_orders(conn)
    assert summary["checked"] == 0 and summary["closed"] == 0


def test_filled_position_gone_from_broker_is_closed(conn, monkeypatch):
    _order(conn, "held-then-gone", "AAPL", status="filled")
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", lambda: [])
    monkeypatch.setattr(reconcile.mcp_client, "list_open_orders", lambda: [])
    summary = reconcile.reconcile_orders(conn)
    assert summary["closed"] == 1
    assert db.list_orders(conn)[0]["status"] == "reconciled-closed"


# --- backfill_positions (D58) -----------------------------------------------

_OCC = "AAPL261009C00330000"


def _pos(symbol=_OCC, qty="2", cost_basis="1360.00", avg="6.80"):
    return {"symbol": symbol, "asset_class": "us_option", "qty": qty,
            "cost_basis": cost_basis, "avg_entry_price": avg}


def test_backfill_reconstructs_an_unknown_broker_position(conn, monkeypatch):
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", lambda: [_pos()])

    summary = reconcile.backfill_positions(conn)
    assert summary["reconstructed"] == 1 and summary["symbols"] == [_OCC]

    rows = db.list_orders(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["contract_symbol"] == _OCC and r["status"] == "broker-reconstructed"
    assert r["reconstructed"] == 1
    assert r["premium"] == 1360.0
    assert r["underlying"] == "AAPL" and r["strike"] == 330.0
    assert "not reconstructed" in r["selection_reason"]
    # points at the one shared synthetic strategy
    strat = db.get_strategy(conn, r["strategy_id"])
    assert strat["dedup_key"] == "__reconstructed__"


def test_backfill_derives_cost_basis_from_avg_price_when_missing(conn, monkeypatch):
    monkeypatch.setattr(reconcile.mcp_client, "list_positions",
                        lambda: [_pos(cost_basis="0", avg="6.80", qty="2")])
    reconcile.backfill_positions(conn)
    assert db.list_orders(conn)[0]["premium"] == 6.80 * 2 * 100


def test_backfill_skips_a_position_already_known_locally(conn, monkeypatch):
    _order(conn, "known", "AAPL", asset_class="option", contract_symbol=_OCC)
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", lambda: [_pos()])
    summary = reconcile.backfill_positions(conn)
    assert summary["reconstructed"] == 0
    assert len(db.list_orders(conn)) == 1


def test_backfill_is_idempotent(conn, monkeypatch):
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", lambda: [_pos()])
    reconcile.backfill_positions(conn)
    reconcile.backfill_positions(conn)
    assert len(db.list_orders(conn)) == 1
    # and only one synthetic strategy exists
    strats = [s for s in db.list_strategies(conn) if s["dedup_key"] == "__reconstructed__"]
    assert len(strats) == 1


def test_backfill_broker_read_failure_changes_nothing(conn, monkeypatch):
    def boom():
        raise RuntimeError("mcp down")
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", boom)
    summary = reconcile.backfill_positions(conn)
    assert summary["reconstructed"] == 0 and summary["skipped"]
    assert db.list_orders(conn) == []


def test_sync_with_broker_backfills_then_reconciles(conn, monkeypatch):
    # local knows about a stale contract the broker no longer holds
    stale = "MSFT261002C00525000"
    _order(conn, "stale", "MSFT", status="filled", asset_class="option",
           contract_symbol=stale)
    # broker holds a different contract, unknown locally
    monkeypatch.setattr(reconcile.mcp_client, "list_positions", lambda: [_pos()])
    monkeypatch.setattr(reconcile.mcp_client, "list_open_orders", lambda: [])

    out = reconcile.sync_with_broker(conn)
    assert out["backfill"]["reconstructed"] == 1
    assert out["reconcile"]["closed"] == 1

    rows = {o["contract_symbol"]: o["status"] for o in db.list_orders(conn)}
    assert rows[_OCC] == "broker-reconstructed"
    assert rows[stale] == "reconciled-closed"
