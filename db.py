"""SQLite persistence for strategies, runs, backtests, and decisions.

Plain ``sqlite3`` — no ORM (see CLAUDE.md scope rules). The schema is created
idempotently by :func:`init_db`, which :func:`connect` calls on every open, so
importing this module and calling ``connect()`` is enough to bootstrap a fresh
database. We reuse the same file as ``data.py``'s bar cache (``bars_cache.db``)
so the whole system is one portable file.

Phase 4 (shadow portfolios / regret ledger) mostly reuses what is here:

  * A rejected candidate already has a row in ``strategies`` (status
    ``rejected``) and its backtest equity curve in ``backtests``. Replaying a
    shadow forward on new bars is just another ``backtests`` row for the same
    ``strategy_id`` — now tagged ``kind = 'forward'`` with the ``as_of`` split.
  * Retiring an active strategy is ``status = 'retired'`` (already an allowed
    value) plus a ``decisions`` row with outcome ``retired``.

What Phase 4 *did* add (see D39): ``backtests.kind`` / ``backtests.as_of`` and
``runs.as_of`` (guarded ``ALTER`` in :func:`_apply_migrations`), and a
``postmortems`` table for the written retirement explanation (T4.4). The earlier
"no migration" note (D18) held for the core shadow mechanic; the as-of split
marker and the post-mortem prose are genuinely new, as D22's tables were.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Same file as data.py's bar cache — one database for the whole system.
# ``DB_PATH`` env var overrides the location (the deployed instance points this
# at a persistent volume, e.g. ``/data/trading.db``); default is the repo file.
DB_PATH = Path(os.getenv("DB_PATH") or Path(__file__).with_name("bars_cache.db"))


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

-- Phase 4 (T4.4): a written post-mortem stored with every retirement. The LLM
-- that writes it is fed ONLY the facts_json numbers (D37) — no free narrative.
CREATE TABLE IF NOT EXISTS postmortems (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id           INTEGER REFERENCES decisions (id),
    run_id                INTEGER REFERENCES runs (id),
    retired_strategy_id   INTEGER NOT NULL REFERENCES strategies (id),
    promoted_strategy_id  INTEGER NOT NULL REFERENCES strategies (id),
    facts_json            TEXT NOT NULL,
    text                  TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies (id),
    run_id      INTEGER REFERENCES runs (id),
    outcome     TEXT NOT NULL CHECK (outcome IN ('promoted', 'rejected', 'retired')),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_strategy ON decisions (strategy_id);

-- Phase 3 (T3.2): every order the agent submits, and which strategy caused it.
-- ``submitted_via`` is always 'mcp' for the agent's real path (D7); the column
-- exists so a reviewer can see it in the row, not just the logs.
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     INTEGER NOT NULL REFERENCES strategies (id),
    run_id          INTEGER REFERENCES runs (id),
    broker_order_id TEXT,                 -- Alpaca order id (NULL for a DRY_RUN or a failure)
    symbol          TEXT NOT NULL,
    qty             REAL,
    side            TEXT NOT NULL,
    status          TEXT NOT NULL,        -- Alpaca status, or 'dry_run' / 'blocked' / 'error'
    submitted_via   TEXT NOT NULL DEFAULT 'mcp' CHECK (submitted_via IN ('mcp', 'sdk')),
    dry_run         INTEGER NOT NULL DEFAULT 0,
    raw_response    TEXT,                 -- raw MCP tool result, kept for audit
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_strategy ON orders (strategy_id);

-- Phase 3 (T3.3): a DB-backed kill switch (the env var is the other way in).
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# Phase 4 (D39): columns added to existing tables. ``CREATE TABLE IF NOT EXISTS``
# never adds a column to a table that already exists, so these are applied with a
# guarded ``ALTER`` — checked against ``PRAGMA table_info`` so it is idempotent
# and safe on the committed seed.db as it is re-seeded onto the Render volume.
#   backtests.kind  — 'primary' (a plain cycle backtest), 'insample' (the as-of
#                     promotion basis) or 'forward' (shadow tracked forward, D34)
#   backtests.as_of — the as-of date a forward/insample split was taken at
#   runs.as_of      — set when a run is an as-of forward-tracking simulation
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("backtests", "kind", "ALTER TABLE backtests ADD COLUMN kind TEXT NOT NULL DEFAULT 'primary'"),
    ("backtests", "as_of", "ALTER TABLE backtests ADD COLUMN as_of TEXT"),
    ("runs", "as_of", "ALTER TABLE runs ADD COLUMN as_of TEXT"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table and index if absent. Idempotent — safe on every startup."""
    conn.executescript(_SCHEMA)
    _apply_migrations(conn)
    conn.commit()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the database, with the schema ensured.

    ``db_path`` defaults to the module-level ``DB_PATH`` (resolved from the
    ``DB_PATH`` env var), read at call time so tests can repoint it.
    ``row_factory`` is ``sqlite3.Row`` so callers get name-addressable rows, and
    foreign keys are enforced.
    """
    conn = sqlite3.connect(db_path or DB_PATH)
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


def set_run_as_of(conn: sqlite3.Connection, run_id: int, as_of: str) -> None:
    """Mark a run as an as-of forward-tracking simulation (Phase 4)."""
    conn.execute("UPDATE runs SET as_of = ? WHERE id = ?", (as_of, run_id))
    conn.commit()


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
    kind: str = "primary",
    as_of: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO backtests
               (strategy_id, run_id, metrics_json, equity_curve_json,
                bars_start, bars_end, kind, as_of, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (strategy_id, run_id, metrics_json, equity_curve_json,
         bars_start, bars_end, kind, as_of, _now()),
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


# --- post-mortems (Phase 4 / T4.4) ------------------------------------------


def insert_postmortem(
    conn: sqlite3.Connection,
    *,
    decision_id: int | None,
    run_id: int | None,
    retired_strategy_id: int,
    promoted_strategy_id: int,
    facts_json: str,
    text: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO postmortems
               (decision_id, run_id, retired_strategy_id, promoted_strategy_id,
                facts_json, text, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (decision_id, run_id, retired_strategy_id, promoted_strategy_id,
         facts_json, text, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_postmortems(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM postmortems ORDER BY id").fetchall()


# --- orders -----------------------------------------------------------------


def insert_order(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    run_id: int | None,
    symbol: str,
    qty: float | None,
    side: str,
    status: str,
    broker_order_id: str | None = None,
    submitted_via: str = "mcp",
    dry_run: bool = False,
    raw_response: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO orders
               (strategy_id, run_id, broker_order_id, symbol, qty, side, status,
                submitted_via, dry_run, raw_response, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (strategy_id, run_id, broker_order_id, symbol, qty, side, status,
         submitted_via, 1 if dry_run else 0, raw_response, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM orders ORDER BY id").fetchall()


# --- system state (DB-backed kill switch, etc.) ----------------------------


def get_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at""",
        (key, value, _now()),
    )
    conn.commit()
