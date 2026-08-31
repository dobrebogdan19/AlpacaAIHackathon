"""mcp_client.py tests — offline (the MCP subprocess is never spawned here).

The real MCP round trip (a paper order visible in the Alpaca dashboard) is
covered by the manual cycle run and by scripts/check_mcp.py, not the suite.
"""

import pytest

import mcp_client


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)


# --- envelope unwrapping ---------------------------------------------------


def test_unwrap_strips_security_envelope():
    payload = {"_alpaca_mcp_security": {"trust": "untrusted"}, "data": {"id": "abc", "status": "accepted"}}
    data, err = mcp_client._unwrap(payload)
    assert err is None
    assert data == {"id": "abc", "status": "accepted"}


def test_unwrap_detects_error_payload():
    data, err = mcp_client._unwrap({"error": {"message": "API rejected the order"}})
    assert err == "API rejected the order"


def test_unwrap_detects_nested_error():
    data, err = mcp_client._unwrap({"data": {"error": {"message": "bad symbol"}}})
    assert err == "bad symbol"


# --- submit_market_order --------------------------------------------------


def test_dry_run_spawns_nothing(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(mcp_client, "_call", lambda *a, **k: called.__setitem__("n", 1))
    res = mcp_client.submit_market_order("AAPL", notional=1000.0, dry_run=True)
    assert res.status == "dry_run"
    assert res.ok is True
    assert called["n"] == 0


def test_requires_exactly_one_sizing():
    with pytest.raises(ValueError):
        mcp_client.submit_market_order("AAPL", dry_run=False)
    with pytest.raises(ValueError):
        mcp_client.submit_market_order("AAPL", qty=1, notional=10, dry_run=False)


def test_successful_order_is_parsed(monkeypatch):
    def fake_acall(tool, args):
        assert tool == mcp_client.PLACE_ORDER_TOOL
        assert args["notional"] == "1000.0"
        payload = {"_alpaca_mcp_security": {}, "data": {"id": "ord-123", "status": "accepted", "qty": "3"}}
        return payload, '{"data": {"id": "ord-123"}}', ["place_stock_order"]

    monkeypatch.setattr(mcp_client, "_call", fake_acall)
    res = mcp_client.submit_market_order("AAPL", notional=1000.0, dry_run=False)
    assert res.ok is True
    assert res.broker_order_id == "ord-123"
    assert res.status == "accepted"
    assert res.via == "mcp"


def test_rejected_order_returns_error(monkeypatch):
    monkeypatch.setattr(
        mcp_client, "_call",
        lambda tool, args: ({"error": {"message": "insufficient buying power"}},
                            "raw", ["place_stock_order"]),
    )
    res = mcp_client.submit_market_order("AAPL", notional=1000.0, dry_run=False)
    assert res.ok is False
    assert res.status == "error"
    assert "insufficient buying power" in res.error


def test_transport_failure_is_surfaced_not_raised(monkeypatch):
    def boom(tool, args):
        raise RuntimeError("MCP server does not expose 'place_stock_order'")

    monkeypatch.setattr(mcp_client, "_call", boom)
    res = mcp_client.submit_market_order("AAPL", notional=1000.0, dry_run=False)
    assert res.ok is False
    assert "does not expose" in res.error


# --- submit_option_order (D48) -------------------------------------------


def test_option_dry_run_spawns_nothing(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(mcp_client, "_call", lambda *a, **k: called.__setitem__("n", 1))
    res = mcp_client.submit_option_order("AAPL260925C00165000", qty=1, dry_run=True)
    assert res.status == "dry_run"
    assert res.ok is True
    assert called["n"] == 0


def test_option_order_is_parsed_and_routes_place_option_order(monkeypatch):
    def fake_call(tool, args):
        assert tool == mcp_client.PLACE_OPTION_ORDER_TOOL
        assert args["symbol"] == "AAPL260925C00165000"
        assert args["qty"] == "2"
        assert args["side"] == "buy"
        assert args["position_intent"] == "buy_to_open"
        assert args["time_in_force"] == "day"
        assert args["type"] == "limit"
        assert args["limit_price"] == "3.40"
        payload = {"data": {"id": "opt-9", "status": "accepted", "qty": "2"}}
        return payload, '{"data":{"id":"opt-9"}}', ["place_option_order"]

    monkeypatch.setattr(mcp_client, "_call", fake_call)
    res = mcp_client.submit_option_order("AAPL260925C00165000", qty=2, limit_price=3.4, dry_run=False)
    assert res.ok is True
    assert res.broker_order_id == "opt-9"
    assert res.status == "accepted"
    assert res.qty == 2.0


def test_option_market_order_when_no_limit_price(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_client, "_call",
                        lambda tool, args: (seen.update(args) or {"data": {"id": "m1", "status": "accepted"}},
                                            "raw", ["place_option_order"]))
    mcp_client.submit_option_order("AAPL260925C00165000", qty=1, dry_run=False)
    assert seen["type"] == "market"
    assert "limit_price" not in seen


def test_option_order_rejected_returns_error(monkeypatch):
    monkeypatch.setattr(
        mcp_client, "_call",
        lambda tool, args: ({"error": {"message": "contract not tradable"}},
                            "raw", ["place_option_order"]),
    )
    res = mcp_client.submit_option_order("AAPL260925C00165000", qty=1, dry_run=False)
    assert res.ok is False
    assert res.status == "error"
    assert "not tradable" in res.error


def test_option_chain_returns_snapshots_and_raises_on_error(monkeypatch):
    monkeypatch.setattr(
        mcp_client, "_call",
        lambda tool, args: ({"data": {"snapshots": {"AAPL260925C00165000": {"latestQuote": {}}}}},
                            "raw", ["get_option_chain"]),
    )
    snaps = mcp_client.option_chain("AAPL", exp_gte="2026-09-29", exp_lte="2026-10-14")
    assert "AAPL260925C00165000" in snaps

    monkeypatch.setattr(
        mcp_client, "_call",
        lambda tool, args: ({"error": {"message": "bad underlying"}}, "raw", ["get_option_chain"]),
    )
    with pytest.raises(RuntimeError):
        mcp_client.option_chain("NOPE")
