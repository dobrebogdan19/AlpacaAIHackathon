"""retire.py — the retirement rule (T4.3). Pure logic, no DB, no broker."""

from retire import RETIREMENT_POLICY, ShadowRecord, find_retirements


def _rec(sid, name, symbol, decision, insample_ret, forward_ret, forward_bars=60,
         forward_trades=12):
    m_in = {"total_return_pct": insample_ret, "max_drawdown_pct": 5.0,
            "num_trades": 12, "win_rate_pct": 50.0, "open_position": False,
            "unrealized_pnl_pct": 0.0}
    m_fw = dict(m_in, total_return_pct=forward_ret, num_trades=forward_trades)
    return ShadowRecord(sid, name, symbol, decision, m_in, m_fw, forward_bars)


def test_retires_when_shadow_beats_losing_active_by_the_margin():
    recs = [
        _rec(1, "active AAPL", "AAPL", "promoted", insample_ret=12.0, forward_ret=-6.0),
        _rec(2, "shadow AAPL", "AAPL", "rejected", insample_ret=1.0, forward_ret=4.0),
    ]
    rts = find_retirements(recs)
    assert len(rts) == 1
    assert rts[0].retired.strategy_id == 1
    assert rts[0].winner.strategy_id == 2
    assert "AAPL" in rts[0].facts["symbol"]


def test_no_retirement_when_active_is_still_profitable():
    """Absolute condition: a winning active is not churned even if a shadow won more."""
    recs = [
        _rec(1, "active", "AAPL", "promoted", 12.0, forward_ret=8.0),
        _rec(2, "shadow", "AAPL", "rejected", 1.0, forward_ret=20.0),
    ]
    assert find_retirements(recs) == []


def test_no_retirement_when_margin_too_small():
    recs = [
        _rec(1, "active", "AAPL", "promoted", 12.0, forward_ret=-2.0),
        _rec(2, "shadow", "AAPL", "rejected", 1.0, forward_ret=2.0),  # only 4pp > -2pp
    ]
    assert find_retirements(recs) == []


def test_no_retirement_when_forward_window_too_short():
    recs = [
        _rec(1, "active", "AAPL", "promoted", 12.0, forward_ret=-6.0),
        _rec(2, "shadow", "AAPL", "rejected", 1.0, forward_ret=8.0,
             forward_bars=int(RETIREMENT_POLICY["min_forward_bars"]) - 1),
    ]
    assert find_retirements(recs) == []


def test_no_retirement_when_the_winning_shadow_never_traded_forward():
    """A shadow that sat in cash all forward is not a demonstrable replacement (D45)."""
    recs = [
        _rec(1, "active", "AAPL", "promoted", 12.0, forward_ret=-6.0),
        _rec(2, "inert shadow", "AAPL", "rejected", 1.0, forward_ret=0.0,
             forward_trades=0),
    ]
    assert find_retirements(recs) == []


def test_retires_only_via_a_shadow_that_actually_traded_forward():
    """Given an inert shadow and a trading one, the retirement uses the trader."""
    recs = [
        _rec(1, "active", "AAPL", "promoted", 12.0, forward_ret=-6.0),
        _rec(2, "inert shadow", "AAPL", "rejected", 1.0, forward_ret=0.0,
             forward_trades=0),
        _rec(3, "trading shadow", "AAPL", "rejected", 1.0, forward_ret=2.0,
             forward_trades=3),
    ]
    rts = find_retirements(recs)
    assert len(rts) == 1
    assert rts[0].winner.strategy_id == 3


def test_shadows_only_matched_within_the_same_symbol():
    recs = [
        _rec(1, "active AAPL", "AAPL", "promoted", 12.0, forward_ret=-6.0),
        _rec(2, "shadow MSFT", "MSFT", "rejected", 1.0, forward_ret=30.0),
    ]
    assert find_retirements(recs) == []


def test_one_shadow_retires_at_most_one_active_the_one_it_beats_widest():
    recs = [
        _rec(1, "active A", "AAPL", "promoted", 12.0, forward_ret=-3.0),
        _rec(2, "active B", "AAPL", "promoted", 12.0, forward_ret=-10.0),
        _rec(3, "shadow", "AAPL", "rejected", 1.0, forward_ret=5.0),
    ]
    rts = find_retirements(recs)
    assert len(rts) == 1
    assert rts[0].retired.strategy_id == 2   # beaten by the wider margin
