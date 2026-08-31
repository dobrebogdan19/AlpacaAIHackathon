"""account.py — live paper-account snapshot: P&L math, caching, stale fallback.

Nothing here touches the network: ``account._fetch_live`` (the one MCP round
trip) is monkeypatched in every test.
"""

import pytest

import account
import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "acct.db")
    yield c
    c.close()


RAW = {
    "account": {
        "account_number": "PA3H59MF72XL", "status": "ACTIVE",
        "portfolio_value": "100412.55", "cash": "40200.00", "equity": "100412.55",
    },
    "positions": [
        {"symbol": "AAPL260116C00200000", "asset_class": "us_option", "qty": "1",
         "avg_entry_price": "3.10", "current_price": "3.80", "market_value": "380",
         "unrealized_pl": "70", "unrealized_plpc": "0.2258"},
        {"symbol": "MSFT260116C00500000", "asset_class": "us_option", "qty": "2",
         "avg_entry_price": "4.00", "current_price": "3.20", "market_value": "640",
         "unrealized_pl": "-160", "unrealized_plpc": "-0.20"},
    ],
}


def test_pnl_is_computed_against_the_starting_balance(conn, monkeypatch):
    monkeypatch.setattr(account, "_fetch_live", lambda: RAW)
    s = account.snapshot(conn)
    assert s["available"] is True and s["stale"] is False and s["source"] == "live"
    assert s["portfolio_value"] == 100412.55
    assert s["total_pnl_abs"] == 412.55
    assert s["total_pnl_pct"] == pytest.approx(0.41255, abs=1e-4)
    assert s["cash"] == 40200.0
    # positions sorted by absolute market value, plpc turned into a percentage
    assert [p["symbol"] for p in s["positions"]] == ["MSFT260116C00500000", "AAPL260116C00200000"]
    assert s["positions"][1]["unrealized_plpc"] == 22.58


def test_second_read_within_ttl_is_served_from_cache(conn, monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return RAW

    monkeypatch.setattr(account, "_fetch_live", fetch)
    account.snapshot(conn)
    again = account.snapshot(conn)
    assert len(calls) == 1
    assert again["source"] == "cache" and again["stale"] is False


def test_failed_live_read_serves_last_known_value_stale(conn, monkeypatch):
    monkeypatch.setattr(account, "_fetch_live", lambda: RAW)
    account.snapshot(conn)  # populate the cache

    def boom():
        raise RuntimeError("MCP subprocess died")

    monkeypatch.setattr(account, "_fetch_live", boom)
    s = account.snapshot(conn, force=True)  # force past the TTL
    assert s["stale"] is True
    assert s["portfolio_value"] == 100412.55        # the last good number
    assert "MCP subprocess died" in s["error"]
    assert s["as_of"]                               # timestamp of the cached snapshot


def test_unavailable_when_no_cache_and_the_read_fails(conn, monkeypatch):
    def boom():
        raise RuntimeError("no key")

    monkeypatch.setattr(account, "_fetch_live", boom)
    s = account.snapshot(conn)
    assert s["available"] is False and s["stale"] is True
    assert s["starting_balance"] == 100_000.0


def test_positions_read_failure_still_yields_balances(monkeypatch, conn):
    import mcp_client

    monkeypatch.setattr(mcp_client, "check_connection", lambda: RAW["account"])

    def boom():
        raise RuntimeError("positions tool timed out")

    monkeypatch.setattr(mcp_client, "list_positions", boom)
    s = account.snapshot(conn)
    assert s["available"] is True
    assert s["portfolio_value"] == 100412.55
    assert s["positions"] == []
