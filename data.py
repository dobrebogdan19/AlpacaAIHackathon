"""Daily-bar fetching with a SQLite cache.

`get_bars(symbol, start, end)` returns a list of bar dicts (oldest first),
each ``{"timestamp": datetime, "open", "high", "low", "close", "volume"}``.

Caching contract (see CLAUDE.md "Data notes"):
  - Bars are cached in SQLite keyed by (symbol, timeframe, date).
  - We separately record which *date ranges* have been queried, so that
    weekends and holidays (no bar) don't look like cache misses.
  - A repeat call for an already-covered range makes ZERO network requests.
  - A partially-overlapping call fetches only the missing sub-range(s).

Every network fetch and every cache hit is logged at INFO.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

log = logging.getLogger("data")

DB_PATH = Path(__file__).with_name("bars_cache.db")

_data_client: StockHistoricalDataClient | None = None


def _client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        load_dotenv()
        _data_client = StockHistoricalDataClient(
            os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        )
    return _data_client


def _tf_key(timeframe: TimeFrame) -> str:
    return f"{timeframe.amount}{timeframe.unit.value}"


def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.fromisoformat(str(d)).date()


def _connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
               symbol TEXT NOT NULL,
               tf     TEXT NOT NULL,
               date   TEXT NOT NULL,
               timestamp TEXT NOT NULL,
               open REAL, high REAL, low REAL, close REAL, volume REAL,
               PRIMARY KEY (symbol, tf, date)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bar_coverage (
               symbol TEXT NOT NULL,
               tf     TEXT NOT NULL,
               start_date TEXT NOT NULL,
               end_date   TEXT NOT NULL
           )"""
    )
    conn.commit()
    return conn


def _covered_intervals(conn, symbol, tf) -> list[tuple[date, date]]:
    rows = conn.execute(
        "SELECT start_date, end_date FROM bar_coverage WHERE symbol=? AND tf=?",
        (symbol, tf),
    ).fetchall()
    intervals = sorted((_as_date(a), _as_date(b)) for a, b in rows)
    merged: list[tuple[date, date]] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _missing_ranges(covered, start: date, end: date) -> list[tuple[date, date]]:
    """Sub-ranges of [start, end] not inside any covered interval."""
    gaps: list[tuple[date, date]] = []
    cursor = start
    for s, e in covered:
        if e < cursor or s > end:
            continue
        if s > cursor:
            gaps.append((cursor, min(s - timedelta(days=1), end)))
        cursor = max(cursor, e + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def _record_coverage(conn, symbol, tf, start: date, end: date) -> None:
    conn.execute(
        "INSERT INTO bar_coverage (symbol, tf, start_date, end_date) VALUES (?,?,?,?)",
        (symbol, tf, start.isoformat(), end.isoformat()),
    )
    conn.commit()


def _fetch_from_alpaca(symbol, start: date, end: date, timeframe: TimeFrame) -> list[dict]:
    """The one place a network request is made. Tests spy on / monkeypatch this."""
    log.info("FETCH %s %s %s..%s", symbol, _tf_key(timeframe), start, end)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe,
        start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        end=datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc),
    )
    resp = _client().get_stock_bars(req)
    if symbol not in resp.data:
        return []
    out = []
    for b in resp.data[symbol]:
        out.append(
            {
                "timestamp": b.timestamp,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            }
        )
    return out


def _store_bars(conn, symbol, tf, bars: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO bars
               (symbol, tf, date, timestamp, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (
                symbol,
                tf,
                b["timestamp"].date().isoformat(),
                b["timestamp"].isoformat(),
                b["open"],
                b["high"],
                b["low"],
                b["close"],
                b["volume"],
            )
            for b in bars
        ],
    )
    conn.commit()


def _read_bars(conn, symbol, tf, start: date, end: date) -> list[dict]:
    rows = conn.execute(
        """SELECT timestamp, open, high, low, close, volume FROM bars
               WHERE symbol=? AND tf=? AND date>=? AND date<=?
               ORDER BY date ASC""",
        (symbol, tf, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        {
            "timestamp": datetime.fromisoformat(ts),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": v,
        }
        for ts, o, h, lo, c, v in rows
    ]


def get_bars(symbol, start, end, timeframe: TimeFrame = TimeFrame.Day, db_path=DB_PATH) -> list[dict]:
    """Return daily bars for `symbol` in [start, end], oldest first.

    Serves cached bars where possible; fetches only the missing date ranges.
    `start` / `end` may be date, datetime, or ISO strings.
    """
    symbol = symbol.upper()
    tf = _tf_key(timeframe)
    start_d, end_d = _as_date(start), _as_date(end)
    if start_d > end_d:
        raise ValueError(f"start {start_d} is after end {end_d}")

    conn = _connect(db_path)
    try:
        covered = _covered_intervals(conn, symbol, tf)
        gaps = _missing_ranges(covered, start_d, end_d)

        if not gaps:
            bars = _read_bars(conn, symbol, tf, start_d, end_d)
            log.info(
                "CACHE HIT %s %s %s..%s (%d bars, 0 fetches)",
                symbol, tf, start_d, end_d, len(bars),
            )
            return bars

        for gap_start, gap_end in gaps:
            fetched = _fetch_from_alpaca(symbol, gap_start, gap_end, timeframe)
            _store_bars(conn, symbol, tf, fetched)
            _record_coverage(conn, symbol, tf, gap_start, gap_end)

        bars = _read_bars(conn, symbol, tf, start_d, end_d)
        log.info(
            "SERVED %s %s %s..%s (%d bars, %d fetch(es) for gaps %s)",
            symbol, tf, start_d, end_d, len(bars), len(gaps), gaps,
        )
        return bars
    finally:
        conn.close()
