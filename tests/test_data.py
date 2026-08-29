"""data.py cache tests — T1.2 acceptance: a repeat call for an already-cached
range makes ZERO network requests; partial overlap fetches only the gap."""

from datetime import date, datetime, timedelta, timezone

import pytest

import data
from alpaca.data.timeframe import TimeFrame


@pytest.fixture
def fake_alpaca(monkeypatch):
    """Replace the one network function with a deterministic generator and a call log."""
    calls = []

    def _fake(symbol, start, end, timeframe):
        calls.append((symbol, start, end))
        bars = []
        d = start
        while d <= end:
            if d.weekday() < 5:  # weekdays only, like a real exchange calendar
                ts = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
                price = 100.0 + d.toordinal() % 10
                bars.append(
                    {"timestamp": ts, "open": price, "high": price + 1,
                     "low": price - 1, "close": price + 0.5, "volume": 1_000.0}
                )
            d += timedelta(days=1)
        return bars

    monkeypatch.setattr(data, "_fetch_from_alpaca", _fake)
    return calls


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_cache.db"


def test_repeat_call_makes_zero_network_requests(fake_alpaca, db_path):
    start, end = date(2025, 1, 6), date(2025, 1, 31)

    first = data.get_bars("AAPL", start, end, TimeFrame.Day, db_path=db_path)
    assert len(fake_alpaca) == 1
    assert len(first) > 0

    second = data.get_bars("AAPL", start, end, TimeFrame.Day, db_path=db_path)
    assert len(fake_alpaca) == 1  # <-- ZERO additional fetches
    assert second == first

    # a sub-range of the cached window is also fully served from cache
    data.get_bars("AAPL", date(2025, 1, 10), date(2025, 1, 20), TimeFrame.Day, db_path=db_path)
    assert len(fake_alpaca) == 1


def test_partial_overlap_fetches_only_the_missing_range(fake_alpaca, db_path):
    data.get_bars("AAPL", date(2025, 1, 6), date(2025, 1, 17), TimeFrame.Day, db_path=db_path)
    assert len(fake_alpaca) == 1

    # extend the window forward — only the new tail should be fetched
    data.get_bars("AAPL", date(2025, 1, 6), date(2025, 1, 31), TimeFrame.Day, db_path=db_path)
    assert len(fake_alpaca) == 2
    _, gap_start, gap_end = fake_alpaca[1]
    assert gap_start == date(2025, 1, 18)
    assert gap_end == date(2025, 1, 31)


def test_bars_are_sorted_oldest_first(fake_alpaca, db_path):
    bars = data.get_bars("AAPL", date(2025, 1, 6), date(2025, 3, 1), TimeFrame.Day, db_path=db_path)
    ts = [b["timestamp"] for b in bars]
    assert ts == sorted(ts)
