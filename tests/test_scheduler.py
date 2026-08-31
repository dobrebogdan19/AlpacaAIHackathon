"""scheduler.py tests (T5.3).

The thread and the real MCP/cycle calls are not exercised — ``tick`` is called
directly with the clock, sweep and entry-cycle seams faked. Focus: every tick
writes exactly one scheduler_ticks row; a closed market does not trade; the
entry cycle is gated by the interval; the startup row records the gap.
"""

import pytest

import db
import scheduler


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "sched.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("SCHEDULER_ENABLED", "SCHEDULER_ENTRY_INTERVAL_MIN", "SCHEDULER_TICK_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(scheduler, "ENTRY_INTERVAL_MIN", 180)


class _Sweep:
    def summary(self):
        return "0 option position(s): 0 closed, 0 held, 0 close-failed"


@pytest.fixture
def _fake_sweep(monkeypatch):
    import mgmt
    monkeypatch.setattr(mgmt, "run_management_sweep", lambda *a, **k: _Sweep())


def _clock(is_open):
    return lambda: {"is_open": is_open, "next_open": "2026-09-01T13:30:00Z",
                    "next_close": None, "timestamp": None}


# --- tick -----------------------------------------------------------------


def test_market_closed_logs_a_tick_and_does_not_trade(conn, monkeypatch, _fake_sweep):
    import mcp_client
    monkeypatch.setattr(mcp_client, "market_clock", _clock(False))
    entry = {"n": 0}
    monkeypatch.setattr(scheduler, "_run_entry_cycle",
                        lambda c: entry.__setitem__("n", entry["n"] + 1) or (0, ""))

    scheduler.tick(conn)

    ticks = db.list_scheduler_ticks(conn)
    assert len(ticks) == 1
    assert ticks[0]["action"] == "skipped-market-closed"
    assert ticks[0]["market_open"] == 0
    assert entry["n"] == 0


def _fake_entry(detail="ran"):
    """A _run_entry_cycle stand-in that still creates a real runs row (FK)."""
    def _f(conn):
        return db.start_run(conn), detail
    return _f


def test_open_market_first_tick_runs_an_entry_cycle(conn, monkeypatch, _fake_sweep):
    import mcp_client
    monkeypatch.setattr(mcp_client, "market_clock", _clock(True))
    monkeypatch.setattr(scheduler, "_run_entry_cycle", _fake_entry("promoted 1"))

    scheduler.tick(conn)

    ticks = db.list_scheduler_ticks(conn)
    assert len(ticks) == 1
    assert ticks[0]["action"] == "entry-cycle"
    assert ticks[0]["run_id"] is not None
    assert "promoted 1" in ticks[0]["detail"]


def test_entry_cycle_is_skipped_within_the_interval(conn, monkeypatch, _fake_sweep):
    import mcp_client
    monkeypatch.setattr(mcp_client, "market_clock", _clock(True))
    monkeypatch.setattr(scheduler, "_run_entry_cycle", _fake_entry())

    scheduler.tick(conn)                       # first: entry-cycle
    calls = {"n": 0}
    monkeypatch.setattr(scheduler, "_run_entry_cycle",
                        lambda c: calls.__setitem__("n", calls["n"] + 1) or (db.start_run(c), "x"))
    scheduler.tick(conn)                       # second, immediately: manage-only

    ticks = db.list_scheduler_ticks(conn)      # DESC
    assert ticks[0]["action"] == "manage-only"
    assert calls["n"] == 0
    assert "entry not due" in ticks[0]["detail"]


def test_entry_cycle_runs_again_after_the_interval_elapses(conn, monkeypatch, _fake_sweep):
    import mcp_client
    monkeypatch.setattr(mcp_client, "market_clock", _clock(True))
    monkeypatch.setattr(scheduler, "ENTRY_INTERVAL_MIN", 0)   # always due
    monkeypatch.setattr(scheduler, "_run_entry_cycle", _fake_entry())

    scheduler.tick(conn)
    scheduler.tick(conn)
    actions = [t["action"] for t in db.list_scheduler_ticks(conn)]
    assert actions == ["entry-cycle", "entry-cycle"]


def test_clock_failure_logs_an_error_tick_and_does_not_trade(conn, monkeypatch, _fake_sweep):
    import mcp_client

    def boom():
        raise RuntimeError("mcp down")
    monkeypatch.setattr(mcp_client, "market_clock", boom)
    monkeypatch.setattr(scheduler, "_run_entry_cycle",
                        lambda c: pytest.fail("must not trade"))

    scheduler.tick(conn)
    ticks = db.list_scheduler_ticks(conn)
    assert len(ticks) == 1 and ticks[0]["action"] == "error"


# --- _entry_due / startup ------------------------------------------------


def test_entry_due_true_when_no_prior_cycle(conn):
    due, why = scheduler._entry_due(conn)
    assert due and "no prior" in why


def test_startup_row_records_the_gap(conn):
    db.insert_scheduler_tick(conn, market_open=True, action="manage-only", detail="x")
    scheduler._log_startup_gap(conn)
    ticks = db.list_scheduler_ticks(conn)
    assert ticks[0]["action"] == "startup"
    assert "gap" in ticks[0]["detail"]


def test_start_is_a_noop_without_the_env_flag(monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    assert scheduler.start() is False


# --- _entry_due derives "last trade" from the broker after a wipe (D58) ------


def test_entry_due_uses_broker_order_history_when_db_is_empty(conn, monkeypatch):
    """DB wiped (no entry-cycle tick), but the broker says we traded 10 min ago —
    the scheduler must NOT re-run the entry cycle."""
    monkeypatch.setenv("BROKER_TRUTH", "1")
    import mcp_client
    from datetime import datetime, timezone, timedelta
    ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(mcp_client, "list_recent_orders",
                        lambda limit=100: [{"submitted_at": ten_min_ago}])

    due, why = scheduler._entry_due(conn)
    assert due is False
    assert "broker" in why


def test_entry_due_broker_read_failure_falls_back_to_db(conn, monkeypatch):
    monkeypatch.setenv("BROKER_TRUTH", "1")
    import mcp_client
    def boom(limit=100):
        raise RuntimeError("mcp down")
    monkeypatch.setattr(mcp_client, "list_recent_orders", boom)

    due, why = scheduler._entry_due(conn)
    assert due is True and "no prior" in why


def test_entry_due_ignores_broker_when_flag_is_off(conn, monkeypatch):
    monkeypatch.delenv("BROKER_TRUTH", raising=False)
    # list_recent_orders must not even be called — no stub provided
    due, why = scheduler._entry_due(conn)
    assert due is True and "no prior" in why
