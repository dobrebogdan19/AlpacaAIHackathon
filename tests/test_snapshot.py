"""DB history snapshot + restore-on-boot (D59).

The Render free tier wipes the DB on every restart. A GitHub Actions job pulls
GET /api/db-snapshot and commits it; on boot the app restores from that file
*only when its own DB is empty*, so a snapshot never clobbers newer live data.
"""

import gzip
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "snap_api.db")
    import api
    conn = db.connect(db.DB_PATH)
    r = db.start_run(conn)
    db.finish_run(conn, r, n_generated=3, n_promoted=1, n_rejected=2)
    db.set_flag(conn, "cache:account", '{"data": {"portfolio_value": 42}, "at": "x"}')
    db.set_flag(conn, "kill_switch", "false")
    conn.close()
    return TestClient(api.app)


def _make_db(path: Path, *, runs: int = 1) -> None:
    import db
    conn = db.connect(path)
    for _ in range(runs):
        db.start_run(conn)
    conn.close()


# --- GET /api/db-snapshot ---------------------------------------------------


def test_snapshot_endpoint_returns_gzipped_sqlite(client):
    r = client.get("/api/db-snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    raw = gzip.decompress(r.content)
    assert raw[:16] == b"SQLite format 3\x00"


def test_snapshot_is_consistent_and_drops_cache_rows(client, tmp_path):
    raw = gzip.decompress(client.get("/api/db-snapshot").content)
    out = tmp_path / "restored.db"
    out.write_bytes(raw)
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    keys = [k for (k,) in conn.execute("SELECT key FROM system_state")]
    assert "kill_switch" in keys                 # real state kept
    assert not any(k.startswith("cache:") for k in keys)   # transient dropped
    conn.close()


def test_snapshot_bytes_are_stable_for_an_unchanged_db(client):
    a = client.get("/api/db-snapshot").content
    b = client.get("/api/db-snapshot").content
    assert a == b            # mtime=0 → identical → workflow can skip a no-op commit


# --- api._restore_from_snapshot ------------------------------------------------


class _FakeResp:
    def __init__(self, data): self._data = data
    def read(self): return self._data
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _serve(monkeypatch, blob):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=None: _FakeResp(blob))


def test_restore_populates_an_empty_db(tmp_path, monkeypatch):
    src = tmp_path / "src.db"
    _make_db(src, runs=3)
    _serve(monkeypatch, gzip.compress(src.read_bytes()))

    import db, api
    target = tmp_path / "target.db"
    monkeypatch.setattr(db, "DB_PATH", target)

    assert api._restore_from_snapshot() is True
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3


def test_restore_never_overwrites_a_non_empty_db(tmp_path, monkeypatch):
    snapshot_src = tmp_path / "snap.db"
    _make_db(snapshot_src, runs=9)
    _serve(monkeypatch, gzip.compress(snapshot_src.read_bytes()))

    import db, api
    live = tmp_path / "live.db"
    _make_db(live, runs=1)                       # one real live run
    monkeypatch.setattr(db, "DB_PATH", live)

    assert api._restore_from_snapshot() is False
    assert sqlite3.connect(live).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_restore_ignores_a_non_sqlite_blob(tmp_path, monkeypatch):
    _serve(monkeypatch, gzip.compress(b"<!doctype html> not a database"))
    import db, api
    target = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", target)
    assert api._restore_from_snapshot() is False


def test_restore_is_silent_when_no_snapshot_exists(tmp_path, monkeypatch):
    def boom(url, timeout=None):
        raise OSError("404")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    import db, api
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    assert api._restore_from_snapshot() is False


# --- the secret scanner ------------------------------------------------------

_SCAN = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "scan_snapshot.py"


def test_secret_scanner_passes_a_clean_db(tmp_path):
    db_path = tmp_path / "clean.db"
    _make_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO strategies (name, symbol, schema_json, created_at, source) "
                 "VALUES ('AAPL 5/20', 'AAPL', '{}', 'now', 'llm')")
    conn.execute("INSERT INTO orders (strategy_id, symbol, side, status, created_at, "
                 "raw_response) VALUES (1, 'AAPL261009C00330000', 'buy', 'filled', 'now', "
                 "'{\"id\": \"2fc715a5-91e4-4d41-ba75-80dfe89a0540\"}')")
    conn.commit(); conn.close()
    r = subprocess.run([sys.executable, str(_SCAN), str(db_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "clean" in r.stdout


def test_secret_scanner_catches_a_planted_key(tmp_path):
    db_path = tmp_path / "leaky.db"
    _make_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO strategies (name, symbol, schema_json, created_at, source, "
                 "raw_llm_output) VALUES ('x','x','{}','now','llm', ?)",
                 ("model said: sk-proj-abcdefghijklmnopqrstuvwxyz123456",))
    conn.commit(); conn.close()
    r = subprocess.run([sys.executable, str(_SCAN), str(db_path)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "SECRET SCAN FAILED" in r.stdout
