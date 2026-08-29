"""Engine tests — behaviour parity with skeleton.py, and lookahead safety.

The regression test is the acceptance criterion for the whole Phase 1 refactor:
engine.run_backtest must reproduce skeleton.py's numbers on the same input.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import skeleton
from engine import run_backtest
from schema import Condition, IndicatorName, IndicatorRef, Operator, Rule, Strategy

FIXTURE = Path(__file__).parent / "fixtures" / "aapl_daily_2026-08-29.json"


def _load_fixture_bars():
    raw = json.loads(FIXTURE.read_text())
    return [
        {
            "timestamp": datetime.fromisoformat(b["timestamp"]),
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": b["volume"],
        }
        for b in raw
    ]


def _sma_crossover_strategy(fast=10, slow=30):
    return Strategy(
        name="AAPL SMA(10/30) crossover",
        symbol="AAPL",
        entry=Rule(
            conditions=[
                Condition(
                    left=IndicatorRef(indicator=IndicatorName.SMA, period=fast),
                    operator=Operator.CROSSES_ABOVE,
                    right=IndicatorRef(indicator=IndicatorName.SMA, period=slow),
                )
            ]
        ),
        exit=Rule(
            conditions=[
                Condition(
                    left=IndicatorRef(indicator=IndicatorName.SMA, period=fast),
                    operator=Operator.CROSSES_BELOW,
                    right=IndicatorRef(indicator=IndicatorName.SMA, period=slow),
                )
            ]
        ),
        rationale="fast SMA crossing the slow SMA is a classic trend-following entry",
    )


def _days(n, start=datetime(2025, 1, 2, tzinfo=timezone.utc)):
    return [start + timedelta(days=i) for i in range(n)]


def _threshold_strategy(period, up_level, down_level):
    """entry: SMA(period) CROSSES_ABOVE up_level ; exit: SMA(period) CROSSES_BELOW down_level."""
    ind = IndicatorRef(indicator=IndicatorName.SMA, period=period)
    return Strategy(
        name="threshold",
        symbol="TEST",
        entry=Rule(conditions=[Condition(left=ind, operator=Operator.CROSSES_ABOVE, right=up_level)]),
        exit=Rule(conditions=[Condition(left=ind, operator=Operator.CROSSES_BELOW, right=down_level)]),
        rationale="synthetic strategy for engine tests",
    )


# --------------------------------------------------------------------------


def test_regression_matches_skeleton_numbers_on_aapl():
    """ACCEPTANCE: engine.py reproduces skeleton.py's AAPL SMA(10/30) result."""
    bars = _load_fixture_bars()

    metrics = run_backtest(_sma_crossover_strategy(), bars)

    assert metrics["total_return_pct"] == 3.84
    assert metrics["max_drawdown_pct"] == 10.78
    assert metrics["num_trades"] == 3
    assert metrics["win_rate_pct"] == 33.33
    assert metrics["open_position"] is False
    assert metrics["unrealized_pnl_pct"] == 0.0

    # ...and it agrees key-for-key with the untouched skeleton implementation
    # run on the identical bars (fully offline parity check).
    skel = skeleton.backtest(bars, skeleton.STRATEGY)
    for key in skel:
        assert metrics[key] == skel[key], key

    # equity curve is present and has one point per bar, for the dashboard.
    assert len(metrics["equity_curve"]) == len(bars)
    assert metrics["equity_curve"][0]["date"] == bars[0]["timestamp"]
    assert metrics["equity_curve"][-1]["date"] == bars[-1]["timestamp"]


def test_execution_uses_next_bar_open_not_decision_close():
    """A crossover on bar N must fill at bar N+1's OPEN, never bar N's close.

    Bars are built so bar N's close (20) and bar N+1's open (100) diverge
    hugely, and price then stays flat at 100 with no exit signal. If the fill
    used bar N+1's open the position is entered at 100 and ends flat
    (~0% return). If it wrongly used bar N's close (20) the position is
    entered 5x cheaper and ends up ~+400%.
    """
    closes = [10, 10, 10, 10, 20, 100, 100, 100, 100]
    opens = [10, 10, 10, 10, 10, 100, 100, 100, 100]  # bar 5 opens at 100 — the gap
    ts = _days(len(closes))
    bars = [
        {"timestamp": ts[i], "open": opens[i], "close": closes[i]}
        for i in range(len(closes))
    ]
    # SMA(2): bar3 -> 10, bar4 -> 15. CROSSES_ABOVE 12 fires at n=4; fills at bar 5 open.
    strat = _threshold_strategy(period=2, up_level=12.0, down_level=1.0)

    metrics = run_backtest(strat, bars)

    assert metrics["open_position"] is True
    assert metrics["num_trades"] == 0
    assert metrics["total_return_pct"] == pytest.approx(0.0, abs=1e-6)
    assert metrics["unrealized_pnl_pct"] == pytest.approx(0.0, abs=1e-6)
    # guard the intent: the wrong (decision-close) fill would be ~+400%
    assert metrics["total_return_pct"] < 1.0


def test_final_bar_signal_is_dropped_not_filled():
    """A signal on the last bar cannot be executed (no bar N+1) and is dropped."""
    closes = [10, 10, 10, 10, 20]   # SMA(2) CROSSES_ABOVE 12 fires at n=4 == last index
    ts = _days(len(closes))
    bars = [{"timestamp": ts[i], "open": closes[i], "close": closes[i]} for i in range(len(closes))]
    strat = _threshold_strategy(period=2, up_level=12.0, down_level=1.0)

    metrics = run_backtest(strat, bars)

    assert metrics["num_trades"] == 0
    assert metrics["open_position"] is False
    assert metrics["total_return_pct"] == 0.0
    assert len(metrics["equity_curve"]) == len(bars)


def test_terminal_open_position_does_not_increment_num_trades():
    """Holding into the final bar is unrealized (D11): reported, but not a trade."""
    closes = [10, 10, 10, 10, 20, 25, 30, 30]
    ts = _days(len(closes))
    bars = [{"timestamp": ts[i], "open": closes[i], "close": closes[i]} for i in range(len(closes))]
    strat = _threshold_strategy(period=2, up_level=12.0, down_level=1.0)  # exit never fires

    metrics = run_backtest(strat, bars)

    assert metrics["open_position"] is True
    assert metrics["num_trades"] == 0
    assert metrics["win_rate_pct"] == 0.0
    assert metrics["unrealized_pnl_pct"] != 0.0  # it IS marked to market


def test_realized_round_trip_counts_as_one_trade():
    """Sanity: an entry followed by an exit signal is one realised trade."""
    #        0   1   2   3   4   5   6   7   8  9  10
    closes = [10, 10, 10, 10, 20, 30, 30, 19, 8, 8, 8]
    ts = _days(len(closes))
    bars = [{"timestamp": ts[i], "open": closes[i], "close": closes[i]} for i in range(len(closes))]
    # SMA(2): CROSSES_ABOVE 12 at n=4 (enter), CROSSES_BELOW 12 at n=8 (exit), both filled next bar.
    strat = _threshold_strategy(period=2, up_level=12.0, down_level=12.0)

    metrics = run_backtest(strat, bars)

    assert metrics["num_trades"] == 1
    assert metrics["open_position"] is False
