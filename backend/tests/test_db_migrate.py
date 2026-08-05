"""backend/db_migrate.py — the deploy-time migration runner backend/main.py's lifespan()
and install.sh's `sudo js` flow both call (see that module's own docstring for the full
"why"). Covers the two pure decision-making helpers plus real end-to-end runs of the
actual `alembic` CLI against a temp SQLite file — this is the piece the task specifically
asked to verify: a migration must apply cleanly to a fresh empty DB and be a true no-op
run twice, and an existing (pre-Alembic) DB must be adopted via `stamp head` rather than
crashing on "table already exists"."""
import sqlite3
from pathlib import Path

from backend.db_migrate import _sqlite_path_from_url, _needs_stamp


# ─── _sqlite_path_from_url ─────────────────────────────────────────────────────────

def test_sqlite_path_from_url_absolute_four_slashes():
    # sqlite+aiosqlite:////data/vrising.db — the DATABASE_URL default used everywhere
    # in this repo (backend/database.py, docker-compose.yml, .env.example).
    assert _sqlite_path_from_url("sqlite+aiosqlite:////data/vrising.db") == Path("/data/vrising.db")


def test_sqlite_path_from_url_relative_three_slashes():
    assert _sqlite_path_from_url("sqlite+aiosqlite:///relative/path.db") == Path("relative/path.db")


def test_sqlite_path_from_url_memory_returns_none():
    assert _sqlite_path_from_url("sqlite+aiosqlite:///:memory:") is None


def test_sqlite_path_from_url_non_sqlite_returns_none():
    assert _sqlite_path_from_url("postgresql://user:pass@host/db") is None


# ─── _needs_stamp ───────────────────────────────────────────────────────────────────

def test_needs_stamp_false_for_nonexistent_file(tmp_path):
    assert _needs_stamp(tmp_path / "does_not_exist.db") is False


def test_needs_stamp_false_for_file_with_no_tables(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()
    assert _needs_stamp(db_path) is False


def test_needs_stamp_true_for_existing_schema_without_alembic_version(tmp_path):
    """The exact shape of a real production DB before this migration system existed:
    real tables, no alembic_version — must be flagged for `stamp head` adoption."""
    db_path = tmp_path / "prod_like.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    assert _needs_stamp(db_path) is True


def test_needs_stamp_false_once_alembic_version_exists(tmp_path):
    db_path = tmp_path / "already_migrated.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    con.commit()
    con.close()
    assert _needs_stamp(db_path) is False


# ─── main() end-to-end (real `alembic` CLI subprocess, real SQLite file) ───────────

def test_main_migrates_fresh_db_and_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")

    from backend import db_migrate
    assert db_migrate.main() == 0
    assert db_path.exists()

    con = sqlite3.connect(str(db_path))
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "alembic_version" in tables
    # spot-check real model tables landed, not just the bookkeeping table
    assert "users" in tables
    assert "poll_votes" in tables

    # re-running against the now-migrated DB must be a true no-op, not an error
    assert db_migrate.main() == 0


def test_main_adopts_existing_unversioned_db_via_stamp_not_upgrade(tmp_path, monkeypatch):
    """Simulates the real production scenario this whole module exists for: a DB that
    already has (some of) the schema from the old create_all()-based approach, but has
    never run Alembic. A plain `alembic upgrade head` against this would try to
    CREATE TABLE users again and fail outright — main() must detect this and stamp
    instead, which does no DDL at all (so the pre-existing lone `users` table stays
    exactly as it was, and none of the OTHER tables a real migration would create ever
    get added — stamping intentionally does not touch the schema)."""
    db_path = tmp_path / "prod_sim.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")

    from backend import db_migrate
    assert db_migrate.main() == 0

    con = sqlite3.connect(str(db_path))
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "alembic_version" in tables
    assert "users" in tables
    assert "poll_votes" not in tables  # stamp did no DDL — only the pre-existing table exists

    # idempotent: a second run just sees alembic_version already present and no-ops
    assert db_migrate.main() == 0
