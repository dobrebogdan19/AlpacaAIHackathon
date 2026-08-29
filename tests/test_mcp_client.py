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
