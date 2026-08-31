"""options.py tests — offline (no chain is ever fetched from Alpaca here).

Covers the selection rules (moneyness ranking, DTE window, two-sided-quote
filter), the OCC symbol parser, the reason string, and the graceful
no-contract path.
"""

from datetime import date

import pytest

import options


TODAY = date(2026, 8, 30)


def _occ(strike: float, expiry: date = date(2026, 10, 2), right: str = "C") -> str:
    return f"AAPL{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def _chain(entries: dict[str, tuple[float, float]]) -> dict:
    """entries: {occ_symbol: (bid, ask)} -> a snapshots-shaped dict."""
    return {sym: {"latestQuote": {"bp": bid, "ap": ask}} for sym, (bid, ask) in entries.items()}


# --- OCC parsing --------------------------------------------------------------


def test_parse_occ_round_trips():
    root, expiry, right, strike = options.parse_occ("AAPL260925C00165000")
    assert root == "AAPL"
    assert expiry == date(2026, 9, 25)
    assert right == "call"
    assert strike == 165.0


def test_parse_occ_put_and_fractional_strike():
    _root, _exp, right, strike = options.parse_occ("SPY260117P00450500")
    assert right == "put"
    assert strike == 450.5


def test_parse_occ_rejects_non_option():
    with pytest.raises(ValueError):
        options.parse_occ("AAPL")


# --- selection ---------------------------------------------------------------


def test_picks_contract_closest_to_target_moneyness():
    spot = 100.0
    # target 1.03 -> want strike ~103. Offer 101, 103, 108.
    chain = _chain({
        _occ(101): (2.0, 2.2),
        _occ(103): (1.4, 1.6),
        _occ(108): (0.7, 0.9),
    })
    choice = options.select_contract("AAPL", spot, chain=chain, today=TODAY)
    assert isinstance(choice, options.ContractChoice)
    assert choice.strike == 103.0
    assert choice.premium == pytest.approx(1.6 * 100)
    assert "closest of" in choice.reason
    assert "+3.0%" in choice.reason  # strike vs spot


def test_dte_window_excludes_near_and_far_expiries():
    spot = 100.0
    chain = _chain({
        _occ(103, date(2026, 9, 5)): (1.0, 1.2),    # ~6 DTE — too soon
        _occ(103, date(2026, 10, 2)): (1.4, 1.6),   # ~33 DTE — in window
        _occ(103, date(2026, 12, 1)): (2.0, 2.4),   # ~93 DTE — too far
    })
    choice = options.select_contract("AAPL", spot, chain=chain, today=TODAY)
    assert isinstance(choice, options.ContractChoice)
    assert choice.expiry == "2026-10-02"


def test_one_sided_quote_is_rejected():
    spot = 100.0
    chain = _chain({
        _occ(103): (0.0, 1.6),     # no bid
        _occ(104): (1.2, 1.4),     # two-sided, slightly further from target
    })
    choice = options.select_contract("AAPL", spot, chain=chain, today=TODAY)
    assert isinstance(choice, options.ContractChoice)
    assert choice.strike == 104.0


def test_no_qualifying_contract_returns_NoContract_with_reason():
    spot = 100.0
    chain = _chain({
        _occ(140): (1.0, 1.2),   # way OTM, outside moneyness tolerance
        _occ(60): (30.0, 31.0),  # way ITM
    })
    choice = options.select_contract("AAPL", spot, chain=chain, today=TODAY)
    assert isinstance(choice, options.NoContract)
    assert choice.underlying == "AAPL"
    assert "moneyness" in choice.reason


def test_empty_chain_is_NoContract_not_crash():
    choice = options.select_contract("AAPL", 100.0, chain={}, today=TODAY)
    assert isinstance(choice, options.NoContract)


def test_invalid_spot_is_NoContract():
    choice = options.select_contract("AAPL", 0.0, chain=_chain({_occ(103): (1, 2)}), today=TODAY)
    assert isinstance(choice, options.NoContract)


def test_fetches_chain_via_mcp_when_not_supplied(monkeypatch):
    captured = {}

    def fake_chain(underlying, **kw):
        captured.update(underlying=underlying, **kw)
        return _chain({_occ(103): (1.4, 1.6)})

    monkeypatch.setattr(options.mcp_client, "option_chain", fake_chain)
    choice = options.select_contract("AAPL", 100.0, today=TODAY)
    assert isinstance(choice, options.ContractChoice)
    assert captured["underlying"] == "AAPL"
    assert captured["contract_type"] == "call"
    assert captured["exp_gte"] and captured["exp_lte"]


def test_chain_fetch_failure_is_NoContract(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("get_option_chain failed: rate limited")

    monkeypatch.setattr(options.mcp_client, "option_chain", boom)
    choice = options.select_contract("AAPL", 100.0, today=TODAY)
    assert isinstance(choice, options.NoContract)
    assert "rate limited" in choice.reason
