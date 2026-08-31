"""Autonomous scheduler (T5.3) — run cycles on their own during market hours.

This is what makes the agent *autonomous*: no one clicks a button. A daemon
thread, started from ``api.py`` when ``SCHEDULER_ENABLED`` is truthy, wakes every
``SCHEDULER_TICK_S`` and:

  1. asks the Alpaca MCP server whether the market is open (``get_clock``).
     Closed → log the tick and stop there. Nothing in the system needs an open
     market to *render*; this is only about when it *acts*.
  2. open → run a **position-management sweep** every tick (``mgmt.py``): open
     option positions are priced and closed if they hit an exit rule. This
     matters more than opening new ones — it runs at the full tick cadence.
  3. open → run a **full entry cycle** (generate → backtest → gate → select
     contract → risk → execute) only if at least ``SCHEDULER_ENTRY_INTERVAL_MIN``
     have passed since the last one. Daily-bar strategies only get new
     information once per day (at the close); a couple of entry cycles per
     trading day covers the fresh bar and gives a retry against a transient
     LLM/MCP failure, without burning the OpenAI budget or over-trading.

Every tick writes a ``scheduler_ticks`` row whether or not it traded — the record
shows the agent was *running*, not only when it acted. On a free-tier cold start
the instance was asleep and the scheduler stopped; the first tick after a restart
logs a ``startup`` row noting the gap, so the record is honest about
non-continuous operation rather than pretending a 24/7 loop.

Keep-alive: Render free spins the web service down after ~15 min with no inbound
HTTP, and a background thread does not count as traffic. Point an external uptime
pinger (e.g. cron-job.org, UptimeRobot — free) at ``/api/health`` every ~10 min
during US market hours to hold it awake. If it sleeps anyway, the gap is logged
and cycles simply resume.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("scheduler")

_TRUTHY = {"1", "true", "yes", "on"}

TICK_INTERVAL_S = int(os.getenv("SCHEDULER_TICK_S", "600"))              # 10 min
ENTRY_INTERVAL_MIN = int(os.getenv("SCHEDULER_ENTRY_INTERVAL_MIN", "180"))  # 3 h
CYCLE_N = int(os.getenv("SCHEDULER_CYCLE_N", "4"))

_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled() -> bool:
    return os.getenv("SCHEDULER_ENABLED", "").strip().lower() in _TRUTHY


def config() -> dict:
    return {
        "enabled": enabled(),
        "tick_interval_s": TICK_INTERVAL_S,
        "entry_interval_min": ENTRY_INTERVAL_MIN,
        "cycle_n": CYCLE_N,
        "running": _thread is not None and _thread.is_alive(),
    }


# --- one tick -----------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _broker_last_trade_at() -> datetime | None:
    """Timestamp of the most recent order on the paper account, via the MCP path.

    This is the durable answer to "when did we last trade?" — it survives a DB
    wipe, unlike ``scheduler_ticks`` (D58). A read failure returns ``None`` and
    the caller falls back to the local marker.
    """
    if not os.getenv("BROKER_TRUTH", "").strip().lower() in _TRUTHY:
        return None
    try:
        import mcp_client

        orders = mcp_client.list_recent_orders(limit=100)
    except Exception as exc:  # noqa: BLE001 — fall back to the local marker
        log.warning("scheduler: broker order-history read failed (%s)", exc)
        return None
    stamps = [
        _parse_dt(o.get("submitted_at") or o.get("created_at"))
        for o in orders
    ]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def _entry_due(conn) -> tuple[bool, str]:
    local = _parse_dt(db.last_entry_cycle_at(conn))
    broker = _broker_last_trade_at()
    last = max([t for t in (local, broker) if t is not None], default=None)

    if last is None:
        return True, "no prior entry cycle (local or broker)"

    delta = datetime.now(timezone.utc) - last
    due = delta >= timedelta(minutes=ENTRY_INTERVAL_MIN)
    mins = int(delta.total_seconds() // 60)
    src = "broker" if (broker is not None and (local is None or broker >= local)) else "local"
    return due, (f"{mins} min since last trade ({src})"
                 f" (interval {ENTRY_INTERVAL_MIN} min)")


def _run_entry_cycle(conn) -> tuple[int, str]:
    import cycle
    from seeds import generate_with_seeds

    run_id = db.start_run(conn)
    try:
        result = cycle.run_cycle(
            n=CYCLE_N, conn=conn, run_id=run_id, generate_fn=generate_with_seeds,
        )
        return run_id, (f"generated {result.n_generated}, promoted {result.n_promoted}, "
                        f"orders {result.n_orders_submitted} "
                        f"(blocked {result.n_orders_blocked}, skipped {result.n_orders_skipped})")
    except Exception as exc:  # noqa: BLE001 — a failed cycle must still close its run row
        log.exception("entry cycle %d failed", run_id)
        try:
            conn.execute(
                "UPDATE runs SET finished_at = ?, n_generated = -1 "
                "WHERE id = ? AND finished_at IS NULL",
                (db._now(), run_id),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        return run_id, f"FAILED: {exc}"


def tick(conn) -> None:
    """One scheduler iteration. Always writes exactly one scheduler_ticks row."""
    import mcp_client
    import mgmt

    try:
        clock = mcp_client.market_clock()
    except Exception as exc:  # noqa: BLE001
        log.warning("tick: market clock unavailable (%s) — not trading", exc)
        db.insert_scheduler_tick(conn, market_open=False, action="error",
                                 detail=f"market clock unavailable: {exc}")
        return

    if not clock["is_open"]:
        db.insert_scheduler_tick(
            conn, market_open=False, action="skipped-market-closed",
            detail=f"next open {clock.get('next_open')}",
        )
        log.info("tick: market closed — next open %s", clock.get("next_open"))
        return

    sweep = mgmt.run_management_sweep(conn)
    due, why = _entry_due(conn)

    if not due:
        db.insert_scheduler_tick(
            conn, market_open=True, action="manage-only",
            detail=f"{sweep.summary()}; entry not due ({why})",
        )
        return

    run_id, cycle_detail = _run_entry_cycle(conn)
    db.insert_scheduler_tick(
        conn, market_open=True, action="entry-cycle", run_id=run_id,
        detail=f"{sweep.summary()}; entry cycle {run_id}: {cycle_detail}",
    )


# --- the loop ----------------------------------------------------------------


def _log_startup_gap(conn) -> None:
    prev = db.last_scheduler_tick(conn)
    if prev is None:
        detail = "first scheduler start on this database"
    else:
        try:
            last = datetime.fromisoformat(str(prev["tick_at"]))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            gap = datetime.now(timezone.utc) - last
            detail = (f"resumed after {int(gap.total_seconds() // 60)} min gap "
                      f"(last tick {prev['tick_at']}, action {prev['action']})")
        except ValueError:
            detail = f"resumed; last tick {prev['tick_at']}"
    db.insert_scheduler_tick(conn, market_open=False, action="startup", detail=detail)
    log.info("scheduler startup — %s", detail)


def _loop() -> None:
    conn = db.connect()
    try:
        _log_startup_gap(conn)
        try:
            import reconcile
            summary = reconcile.sync_with_broker(conn)
            log.info("scheduler startup sync: %s position(s) reconstructed, "
                     "%s stale row(s) closed",
                     summary["backfill"]["reconstructed"], summary["reconcile"]["closed"])
        except Exception:  # noqa: BLE001
            log.exception("startup broker sync failed — continuing")

        while not _stop.is_set():
            try:
                tick(conn)
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("scheduler tick crashed")
                try:
                    db.insert_scheduler_tick(conn, market_open=False, action="error",
                                             detail="tick raised — see logs")
                except Exception:  # noqa: BLE001
                    pass
            _stop.wait(TICK_INTERVAL_S)
    finally:
        conn.close()
        log.info("scheduler loop exited")


def start() -> bool:
    """Start the scheduler thread if ``SCHEDULER_ENABLED``. Idempotent."""
    global _thread
    if not enabled():
        log.info("scheduler disabled (set SCHEDULER_ENABLED=1 to run it)")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    _thread.start()
    log.info("scheduler started — tick %ds, entry every %dm",
             TICK_INTERVAL_S, ENTRY_INTERVAL_MIN)
    return True


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
