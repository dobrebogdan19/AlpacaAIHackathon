"""SQLite persistence for strategies, runs, backtests, and decisions.

Plain ``sqlite3`` — no ORM (see CLAUDE.md scope rules). The schema is created
idempotently by :func:`init_db`, which :func:`connect` calls on every open, so
importing this module and calling ``connect()`` is enough to bootstrap a fresh
database. We reuse the same file as ``data.py``'s bar cache (``bars_cache.db``)
so the whole system is one portable file.

Phase 4 (shadow portfolios / regret ledger) attaches here **without a
migration**:

  * A rejected candidate already has a row in ``strategies`` (status
    ``rejected``) and its backtest equity curve in ``backtests``. Replaying a
    shadow forward on new bars is just another ``backtests`` row for the same
    ``strategy_id`` (a later ``run_id``, a later ``bars_end``).
  * Retiring an active strategy is ``status = 'retired'`` (already an allowed
    value) plus a ``decisions`` row with outcome ``retired``.
  * ``decisions`` and ``backtests`` therefore need no new columns for shadows;
    the CHECK constraints below already permit the Phase 4 values.

No shadow-specific tables are created yet — that is Phase 4's job.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Same file as data.py's bar cache — one database for the whole system.
DB_PATH = Path(__file__).with_name("bars_cache.db")


def _now() -> str:
    """UTC timestamp, ISO-8601, used for every ``created_at`` / ``*_at`` value."""
    return datetime.now(timezone.utc).isoformat()


# --- schema ---------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    schema_json    TEXT NOT NULL,               -- the validated Strategy, as JSON
    rationale      TEXT,
    created_at     TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('llm', 'manual')),
    status         TEXT NOT NULL DEFAULT 'candidate'
                        CHECK (status IN ('candidate', 'active', 'rejected', 'retired')),
    raw_llm_output TEXT,                          -- raw model text, kept for audit (NULL for manual)
    dedup_key      TEXT                           -- canonical hash; see generator.dedup_key()
);

-- One canonical strategy per (symbol + semantic rules). Partial index so that
-- manually entered strategies (dedup_key NULL) are never blocked.
CREATE UNIQUE INDEX IF NOT EXISTS ix_strategies_dedup
    ON strategies (dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    n_generated  INTEGER,
    n_promoted   INTEGER,
    n_rejected   INTEGER
);

CREATE TABLE IF NOT EXISTS backtests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id       INTEGER NOT NULL REFERENCES strategies (id),
    run_id            INTEGER REFERENCES runs (id),
    metrics_json      TEXT NOT NULL,
    equity_curve_json TEXT NOT NULL,
    bars_start        TEXT,
    bars_end          TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_backtests_strategy ON backtests (strategy_id);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies (id),
    run_id      INTEGER REFERENCES runs (id),
    outcome     TEXT NOT NULL CHECK (outcome IN ('promoted', 'rejected', 'retired')),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_strategy ON decisions (strategy_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table and index if absent. Idempotent — safe on every startup."""
    conn.executescript(_SCHEMA)
    conn.commit()


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the database, with the schema ensured.

    ``row_factory`` is ``sqlite3.Row`` so callers get name-addressable rows, and
    foreign keys are enforced.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


# --- strategies ---------------------------------------------------------------


def insert_strategy(
    conn: sqlite3.Connection,
    *,
    name: str,
    symbol: str,
    schema_json: str,
    rationale: str | None,
    source: str,
    status: str = "candidate",
    raw_llm_output: str | None = None,
    dedup_key: str | None = None,
) -> int:
    """Insert a strategy and return its id.

    If ``dedup_key`` matches an existing row, nothing is inserted and the id of
    the existing strategy is returned — so callers can persist a whole generated
    batch without worrying about collisions (T2.3).
    """
    if dedup_key is not None:
        row = conn.execute(
            "SELECT id FROM strategies WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if row is not None:
            return int(row["id"])

    cur = conn.execute(
        """INSERT INTO strategies
               (name, symbol, schema_json, rationale, created_at, source, status,
                raw_llm_output, dedup_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, symbol, schema_json, rationale, _now(), source, status,
         raw_llm_output, dedup_key),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_strategy(conn: sqlite3.Connection, strategy_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
    ).fetchone()


def list_strategies(
    conn: sqlite3.Connection, status: str | None = None
) -> list[sqlite3.Row]:
    if status is None:
        return conn.execute(
            "SELECT * FROM strategies ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM strategies WHERE status = ? ORDER BY id", (status,)
    ).fetchall()


def count_strategies(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0])


def existing_dedup_keys(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT dedup_key FROM strategies WHERE dedup_key IS NOT NULL"
    ).fetchall()
    return {r["dedup_key"] for r in rows}


def set_strategy_status(conn: sqlite3.Connection, strategy_id: int, status: str) -> None:
    conn.execute(
        "UPDATE strategies SET status = ? WHERE id = ?", (status, strategy_id)
    )
    conn.commit()


# --- runs -------------------------------------------------------------------


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    n_generated: int,
    n_promoted: int,
    n_rejected: int,
) -> None:
    conn.execute(
        """UPDATE runs
               SET finished_at = ?, n_generated = ?, n_promoted = ?, n_rejected = ?
             WHERE id = ?""",
        (_now(), n_generated, n_promoted, n_rejected, run_id),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


# --- backtests -------------------------------------------------------------


def insert_backtest(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    run_id: int | None,
    metrics_json: str,
    equity_curve_json: str,
    bars_start: str | None = None,
    bars_end: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO backtests
               (strategy_id, run_id, metrics_json, equity_curve_json,
                bars_start, bars_end, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (strategy_id, run_id, metrics_json, equity_curve_json,
         bars_start, bars_end, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- decisions -----------------------------------------------------------------


def insert_decision(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    run_id: int | None,
    outcome: str,
    reason: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO decisions (strategy_id, run_id, outcome, reason, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (strategy_id, run_id, outcome, reason, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_decisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
