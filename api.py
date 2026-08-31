"""Read-only HTTP surface over the stored trading record, plus an on-demand cycle.

Every GET reads only persisted rows — no live market, no scheduler, no network
call is required to render anything (DECISIONS.md D6). ``POST /api/cycle`` is the
one write path: it runs a full ``generate -> backtest -> gate -> execute`` cycle
in a background task and returns immediately with a run id to poll.

Run locally:
    uvicorn api:app --reload
Deployed (Railway): see the Procfile. ``DB_PATH`` points at the persistent
volume; ``DRY_RUN`` is unset (live paper orders through the Alpaca MCP server).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

_STATIC = Path(__file__).with_name("static")

# --- one cycle at a time, and not more than one per CYCLE_MIN_INTERVAL_S ------
CYCLE_MIN_INTERVAL_S = int(os.getenv("CYCLE_MIN_INTERVAL_S", "60"))
_cycle_lock = threading.Lock()
_cycle_state = {"running": False, "last_started": 0.0, "last_run_id": None}


def _bootstrap_db() -> None:
    """On first boot against an empty volume, seed from the committed ``seed.db``.

    Only runs when ``DB_PATH`` is set explicitly (i.e. a deploy pointing at a
    persistent volume). Local dev, with no ``DB_PATH``, uses ``bars_cache.db`` as
    it finds it — this never overwrites a developer's working database.
    """
    if not os.getenv("DB_PATH"):
        return
    if os.getenv("SKIP_SEED_BOOTSTRAP", "").strip().lower() in {"1", "true", "yes", "on"}:
        # The competition instance trades a fresh dedicated paper account and
        # starts from an empty database; the scheduler's startup reconciliation
        # (reconcile.py) syncs order/position state from that account (D49).
        log.info("SKIP_SEED_BOOTSTRAP set — starting with an empty database")
        return
    target = db.DB_PATH
    if target.exists() and target.stat().st_size > 0:
        return
    seed = Path(__file__).with_name("seed.db")
    if seed.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(seed, target)
        log.info("bootstrapped %s from seed.db (%d bytes)", target, target.stat().st_size)
    else:
        log.info("no seed.db present; starting with an empty database at %s", target)


_bootstrap_db()


@asynccontextmanager
async def _lifespan(_app):
    """Kick off the autonomous scheduler (T5.3) if SCHEDULER_ENABLED is set.

    No-op locally and in tests (the var is unset), so importing ``api`` never
    spawns a background thread or touches the MCP server on its own.
    """
    import scheduler

    try:
        scheduler.start()
    except Exception:  # noqa: BLE001 — a scheduler failure must not stop the API
        log.exception("scheduler failed to start — API still serving")
    try:
        yield
    finally:
        try:
            scheduler.stop()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title="Self-auditing trading agent", version="1.0", lifespan=_lifespan)


# --- helpers ----------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    return db.connect()


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(conn, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r is not None else None


def _loads(s: str | None):
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


# --- read-only endpoints --------------------------------------------------


@app.get("/api/health")
def health():
    import risk

    conn = _conn()
    try:
        engaged, why = risk.kill_switch_engaged(conn)
        return {
            "status": "ok",
            "db_path": str(db.DB_PATH),
            "dry_run": risk.dry_run_active(None),
            "kill_switch": engaged,
            "kill_switch_reason": why or None,
            "counts": {
                "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "strategies": conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0],
                "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            },
            "cycle": {
                "running": _cycle_state["running"],
                "last_run_id": _cycle_state["last_run_id"],
            },
        }
    finally:
        conn.close()


@app.get("/api/runs")
def list_runs():
    conn = _conn()
    try:
        runs = _rows(
            conn,
            """SELECT r.*,
                      (SELECT COUNT(*) FROM decisions d WHERE d.run_id = r.id) AS n_decisions,
                      (SELECT COUNT(*) FROM orders o WHERE o.run_id = r.id) AS n_orders
                 FROM runs r ORDER BY r.id DESC""",
        )
        return {"runs": runs}
    finally:
        conn.close()


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    conn = _conn()
    try:
        run = _row(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise HTTPException(404, f"no run {run_id}")

        # one row per candidate seen in this run: its decision + the backtest it
        # was judged on.
        candidates = []
        decisions = _rows(
            conn,
            """SELECT d.*, s.name, s.symbol, s.source, s.status
                 FROM decisions d JOIN strategies s ON s.id = d.strategy_id
                WHERE d.run_id = ? ORDER BY d.id""",
            (run_id,),
        )
        for d in decisions:
            bt = _row(
                conn,
                """SELECT metrics_json, equity_curve_json, bars_start, bars_end, kind
                     FROM backtests
                    WHERE run_id = ? AND strategy_id = ?
                    ORDER BY (kind = 'primary') DESC, (kind = 'insample') DESC, id DESC
                    LIMIT 1""",
                (run_id, d["strategy_id"]),
            )
            candidates.append({
                "strategy_id": d["strategy_id"],
                "name": d["name"],
                "symbol": d["symbol"],
                "source": d["source"],
                "status": d["status"],
                "outcome": d["outcome"],
                "reason": d["reason"],
                "decided_at": d["created_at"],
                "metrics": _loads(bt["metrics_json"]) if bt else None,
                "bars_start": bt["bars_start"] if bt else None,
                "bars_end": bt["bars_end"] if bt else None,
                "metrics_kind": bt["kind"] if bt else None,
            })

        orders = _rows(
            conn,
            """SELECT o.*, s.name AS strategy_name
                 FROM orders o JOIN strategies s ON s.id = o.strategy_id
                WHERE o.run_id = ? ORDER BY o.id""",
            (run_id,),
        )
        return {"run": run, "candidates": candidates, "orders": orders}
    finally:
        conn.close()


@app.get("/api/strategies")
def list_strategies():
    conn = _conn()
    try:
        strategies = _rows(conn, "SELECT * FROM strategies ORDER BY id")
        out = []
        for s in strategies:
            bt = _row(
                conn,
                """SELECT metrics_json, bars_start, bars_end, run_id
                     FROM backtests WHERE strategy_id = ? AND kind = 'primary'
                     ORDER BY id DESC LIMIT 1""",
                (s["id"],),
            )
            last_decision = _row(
                conn,
                "SELECT outcome, reason, created_at FROM decisions WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
                (s["id"],),
            )
            out.append({
                "id": s["id"],
                "name": s["name"],
                "symbol": s["symbol"],
                "source": s["source"],
                "status": s["status"],
                "created_at": s["created_at"],
                "rationale": s["rationale"],
                "latest_metrics": _loads(bt["metrics_json"]) if bt else None,
                "latest_decision": last_decision,
            })
        return {"strategies": out}
    finally:
        conn.close()


@app.get("/api/strategies/{strategy_id}")
def get_strategy(strategy_id: int):
    conn = _conn()
    try:
        s = _row(conn, "SELECT * FROM strategies WHERE id = ?", (strategy_id,))
        if s is None:
            raise HTTPException(404, f"no strategy {strategy_id}")
        backtests = _rows(
            conn,
            """SELECT id, run_id, metrics_json, equity_curve_json, bars_start, bars_end, created_at
                 FROM backtests WHERE strategy_id = ? ORDER BY id""",
            (strategy_id,),
        )
        for bt in backtests:
            bt["metrics"] = _loads(bt.pop("metrics_json"))
            bt["equity_curve"] = _loads(bt.pop("equity_curve_json"))
        decisions = _rows(
            conn,
            "SELECT * FROM decisions WHERE strategy_id = ? ORDER BY id",
            (strategy_id,),
        )
        orders = _rows(
            conn,
            "SELECT * FROM orders WHERE strategy_id = ? ORDER BY id",
            (strategy_id,),
        )
        return {
            "strategy": {
                "id": s["id"],
                "name": s["name"],
                "symbol": s["symbol"],
                "source": s["source"],
                "status": s["status"],
                "created_at": s["created_at"],
                "rationale": s["rationale"],
                "schema": _loads(s["schema_json"]),
                "raw_llm_output": s["raw_llm_output"],
            },
            "backtests": backtests,
            "decisions": decisions,
            "orders": orders,
        }
    finally:
        conn.close()


@app.get("/api/orders")
def list_orders():
    conn = _conn()
    try:
        orders = _rows(
            conn,
            """SELECT o.*, s.name AS strategy_name, s.symbol AS strategy_symbol
                 FROM orders o JOIN strategies s ON s.id = o.strategy_id
                ORDER BY o.id DESC""",
        )
        return {"orders": orders}
    finally:
        conn.close()


@app.get("/api/equity-curves")
def equity_curves():
    """Latest equity curve for every promoted (active/retired) strategy — the hero plot."""
    conn = _conn()
    try:
        promoted = _rows(
            conn,
            "SELECT id, name, symbol, status FROM strategies WHERE status IN ('active', 'retired') ORDER BY id",
        )
        series = []
        for s in promoted:
            bt = _row(
                conn,
                """SELECT equity_curve_json, metrics_json FROM backtests
                    WHERE strategy_id = ? AND kind = 'primary'
                    ORDER BY id DESC LIMIT 1""",
                (s["id"],),
            )
            if not bt:
                continue
            curve = _loads(bt["equity_curve_json"]) or []
            series.append({
                "strategy_id": s["id"],
                "name": s["name"],
                "symbol": s["symbol"],
                "status": s["status"],
                "metrics": _loads(bt["metrics_json"]),
                "points": [
                    {"date": str(p.get("date"))[:10], "equity": p.get("equity")}
                    for p in curve
                ],
            })
        return {"series": series}
    finally:
        conn.close()


# --- Phase 4: the regret ledger (all pure SELECTs) -----------------------


def _latest_as_of_run(conn) -> dict | None:
    return _row(
        conn,
        "SELECT * FROM runs WHERE as_of IS NOT NULL ORDER BY id DESC LIMIT 1",
    )


@app.get("/api/shadow-curves")
def shadow_curves():
    """Forward-tracked equity curves for the latest as-of run — the hero plot.

    Every evaluated strategy's forward curve (measured from the as-of decision
    date), tagged with its CURRENT status so the dashboard can plot active
    strategies among the shadows. This is a historical simulation of forward
    tracking, not live results — ``as_of`` and ``simulation`` say so.
    """
    conn = _conn()
    try:
        run = _latest_as_of_run(conn)
        if run is None:
            return {"as_of": None, "simulation": True, "series": []}
        rows = _rows(
            conn,
            """SELECT b.strategy_id, b.metrics_json, b.equity_curve_json,
                      b.bars_start, b.bars_end, s.name, s.symbol, s.source, s.status
                 FROM backtests b JOIN strategies s ON s.id = b.strategy_id
                WHERE b.run_id = ? AND b.kind = 'forward'
                ORDER BY b.strategy_id""",
            (run["id"],),
        )
        decisions = {
            d["strategy_id"]: d["outcome"]
            for d in _rows(conn,
                           "SELECT strategy_id, outcome FROM decisions WHERE run_id = ? "
                           "AND outcome IN ('promoted','rejected')", (run["id"],))
        }
        series = []
        for r in rows:
            curve = _loads(r["equity_curve_json"]) or []
            series.append({
                "strategy_id": r["strategy_id"],
                "name": r["name"],
                "symbol": r["symbol"],
                "source": r["source"],
                "status": r["status"],
                "as_of_decision": decisions.get(r["strategy_id"]),
                "tracking_start": r["bars_start"],
                "metrics": _loads(r["metrics_json"]),
                "points": [
                    {"date": str(p.get("date"))[:10], "equity": p.get("equity")}
                    for p in curve
                ],
            })
        return {"as_of": run["as_of"], "simulation": True, "series": series}
    finally:
        conn.close()


@app.get("/api/selection-bias")
def selection_bias():
    """T4.5 — mean forward return of as-of-promoted vs as-of-rejected candidates.

    Computed from stored rows of the latest as-of run. Reported as-is with the
    sample size, whatever it says (D10).
    """
    conn = _conn()
    try:
        run = _latest_as_of_run(conn)
        if run is None:
            return {"as_of": None, "available": False,
                    "note": "no as-of evaluation has been run yet"}
        rows = _rows(
            conn,
            """SELECT d.outcome, b.metrics_json
                 FROM decisions d
                 JOIN backtests b
                   ON b.strategy_id = d.strategy_id AND b.run_id = d.run_id
                      AND b.kind = 'forward'
                WHERE d.run_id = ? AND d.outcome IN ('promoted','rejected')""",
            (run["id"],),
        )
        buckets: dict[str, list[float]] = {"promoted": [], "rejected": []}
        for r in rows:
            m = _loads(r["metrics_json"]) or {}
            if "total_return_pct" in m:
                buckets[r["outcome"]].append(float(m["total_return_pct"]))
        def avg(xs):
            return round(sum(xs) / len(xs), 2) if xs else None
        prom, rej = avg(buckets["promoted"]), avg(buckets["rejected"])
        spread = round(prom - rej, 2) if prom is not None and rej is not None else None
        return {
            "as_of": run["as_of"],
            "available": spread is not None,
            "promoted_avg_forward_return_pct": prom,
            "rejected_avg_forward_return_pct": rej,
            "n_promoted": len(buckets["promoted"]),
            "n_rejected": len(buckets["rejected"]),
            "spread_pp": spread,
            "note": (
                "Average forward return of strategies the gate would have promoted "
                "at the as-of date, minus that of the ones it rejected, over bars "
                "the gate never saw. A spread near zero means the gate is not "
                "distinguishing signal from noise. Small sample — read with the n."
            ),
        }
    finally:
        conn.close()


@app.get("/api/calibration")
def calibration():
    """The latest gate recalibration proposal (calibrate.py).

    A proposal derived by in-sample optimisation over forward returns, with a
    holdout check that says how much of the gain survives. Stored, never
    auto-applied — ``gate.py`` is unchanged whatever this says (D10).
    """
    conn = _conn()
    try:
        row = db.latest_calibration(conn)
        if row is None:
            return {"available": False,
                    "note": "no calibration has been run yet"}
        return {
            "available": True,
            "id": row["id"],
            "applied": bool(row["applied"]),
            "created_at": row["created_at"],
            "record": _loads(row["record_json"]),
        }
    finally:
        conn.close()


@app.get("/api/retirements")
def retirements():
    """Every retirement the regret ledger triggered, with its post-mortem."""
    conn = _conn()
    try:
        pms = _rows(
            conn,
            """SELECT p.*, r.name AS retired_name, r.symbol AS symbol,
                      w.name AS promoted_name, d.reason AS reason, d.created_at AS retired_at
                 FROM postmortems p
                 JOIN strategies r ON r.id = p.retired_strategy_id
                 JOIN strategies w ON w.id = p.promoted_strategy_id
                 LEFT JOIN decisions d ON d.id = p.decision_id
                ORDER BY p.id DESC""",
        )
        for p in pms:
            p["facts"] = _loads(p.pop("facts_json"))
            close = _row(
                conn,
                """SELECT status FROM orders
                    WHERE strategy_id = ? AND run_id = ? AND side = 'sell'
                    ORDER BY id DESC LIMIT 1""",
                (p["retired_strategy_id"], p["run_id"]),
            )
            p["close_status"] = close["status"] if close else None
        return {"retirements": pms}
    finally:
        conn.close()


# --- the one write path ---------------------------------------------------


def _run_cycle_bg(run_id: int) -> None:
    import cycle
    from seeds import generate_with_seeds

    conn = db.connect()
    try:
        result = cycle.run_cycle(
            n=4, conn=conn, run_id=run_id, generate_fn=generate_with_seeds,
        )
        log.info("cycle %d done: %s", run_id, result.summary().splitlines()[0])
    except Exception:  # noqa: BLE001 — a failed cycle must still close out its run row
        log.exception("cycle %d failed", run_id)
        try:
            conn.execute(
                "UPDATE runs SET finished_at = ?, n_generated = -1 WHERE id = ? AND finished_at IS NULL",
                (db._now(), run_id),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()
        with _cycle_lock:
            _cycle_state["running"] = False


@app.post("/api/cycle")
def trigger_cycle(background_tasks: BackgroundTasks):
    now = time.monotonic()
    with _cycle_lock:
        if _cycle_state["running"]:
            raise HTTPException(429, "a cycle is already running")
        wait = CYCLE_MIN_INTERVAL_S - (now - _cycle_state["last_started"])
        if _cycle_state["last_started"] and wait > 0:
            raise HTTPException(429, f"rate limited — try again in {int(wait) + 1}s")
        conn = db.connect()
        try:
            run_id = db.start_run(conn)
        finally:
            conn.close()
        _cycle_state.update(running=True, last_started=now, last_run_id=run_id)

    background_tasks.add_task(_run_cycle_bg, run_id)
    return JSONResponse({"run_id": run_id, "poll": f"/api/runs/{run_id}"}, status_code=202)


@app.get("/api/scheduler")
def scheduler_status():
    """The autonomous scheduler's config and its recent tick log (T5.3).

    Pure SELECT over ``scheduler_ticks`` plus the in-process config — rendering
    this never needs the scheduler to be running (D6).
    """
    import scheduler

    conn = _conn()
    try:
        ticks = _rows(conn,
                      "SELECT * FROM scheduler_ticks ORDER BY id DESC LIMIT 100")
        counts = _rows(conn,
                       "SELECT action, COUNT(*) AS n FROM scheduler_ticks GROUP BY action")
        return {
            "config": scheduler.config(),
            "last_entry_cycle_at": db.last_entry_cycle_at(conn),
            "tick_counts": {r["action"]: r["n"] for r in counts},
            "ticks": ticks,
        }
    finally:
        conn.close()


@app.get("/api/mcp-check")
def mcp_check():
    """Explicit round trip through the Alpaca MCP server. Not called on page load
    (it spawns a subprocess) — used to verify the MCP path after a deploy (T7.2)."""
    import mcp_client

    try:
        data = mcp_client.check_connection()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"MCP path failed: {exc}")
    return {
        "ok": True,
        "account_number": data.get("account_number"),
        "status": data.get("status"),
        "buying_power": data.get("buying_power"),
    }


# --- dashboard ----------------------------------------------------------------


@app.get("/")
def dashboard():
    return FileResponse(_STATIC / "index.html")
