"""Deploy-time migration runner for the Alembic history in alembic/.

Single source of truth for "how to safely bring the DB schema up to date" —
both backend/main.py's lifespan() (every process boot, via a subprocess so
Alembic's own logging setup in alembic/env.py — which calls fileConfig() on
alembic.ini — can never clobber this app's own logging config) and
install.sh's deploy flow (`sudo js`, via `docker compose exec ... python -m
backend.db_migrate`) call this exact same module rather than duplicating the
decision logic in two places.

Why this isn't just a plain `alembic upgrade head`
----------------------------------------------------
Every deployment of this site predating this module already has the full
41-table schema — built over time by backend/main.py's old
`Base.metadata.create_all()` plus a long, hand-maintained list of raw
`ALTER TABLE`/`CREATE TABLE`/`CREATE INDEX` statements that used to live in
`lifespan()` (see git history) — but has *never* run a single Alembic
migration, so it has no `alembic_version` table yet. Running a plain
`alembic upgrade head` against a DB in that state would try to `CREATE TABLE`
every table the initial migration defines and fail immediately on the very
first one ("table already exists").

The standard fix for adopting Alembic into an already-schema'd, unversioned
database is `alembic stamp head`: it only writes the bookkeeping row into
`alembic_version` (creating that table if needed) — no DDL, no comparison
against the DB's actual columns/indexes/constraints — recording "this DB is
already at head". This module detects exactly that situation (real tables
present, no `alembic_version` yet) and stamps once before deferring to a
normal `upgrade head`, which then correctly becomes a no-op for that first
run and behaves normally (applies whatever's new) on every deploy after.

A truly fresh/empty DB (no tables at all, e.g. a brand new install) skips the
stamp and goes straight to `alembic upgrade head`, which creates the full
schema from the initial migration onward — same end state as the old
create_all() + raw-ALTER approach used to produce, just versioned now.

Known, accepted gap: on a DB adopted via `stamp head`, a handful of indexes/
one constraint the old raw-ALTER block created carry different SQLite-internal
names than what building the same schema fresh via the migrations would
produce (e.g. `ix_comments_author` vs the model-derived `ix_comments_author_id`;
the old raw `poll_votes` UNIQUE(poll_id, option_id, user_id) rebuild has no
name at all vs the migration's explicit `uq_poll_vote`). Functionally
identical (same columns, same uniqueness enforcement) — cosmetic only, and not
worth a data-shuffling migration to chase down. Fresh installs don't have this
gap: they get the model-derived names from the start.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sqlite_path_from_url(url: str) -> Optional[Path]:
    """Extract a filesystem path from a sqlite(+aiosqlite):/// URL, or None for
    a non-sqlite DATABASE_URL or an in-memory DB — the adoption check below only
    makes sense for a real file on disk."""
    if not url.startswith("sqlite"):
        return None
    # sqlite+aiosqlite:////absolute/path.db  (4 slashes: absolute path)
    # sqlite+aiosqlite:///relative/path.db   (3 slashes: relative path)
    # sqlite+aiosqlite:///:memory:           (no real file)
    raw = url.split("///", 1)[-1]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def _needs_stamp(db_path: Path) -> bool:
    """True if db_path is an existing SQLite file with real tables but no
    alembic_version table yet — i.e. a pre-Alembic DB that needs one-time
    adoption via `stamp head` before `upgrade head` can run safely."""
    if not db_path.exists():
        return False  # nothing on disk yet — straight to a normal upgrade
    con = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        con.close()
    return bool(tables) and "alembic_version" not in tables


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////data/vrising.db")
    db_path = _sqlite_path_from_url(database_url)

    if db_path is not None and _needs_stamp(db_path):
        print(f"[db_migrate] {db_path} has existing tables but no alembic_version yet — "
              f"adopting in place via 'alembic stamp head' (bookkeeping only, no DDL).", flush=True)
        rc = subprocess.run(["alembic", "stamp", "head"], cwd=str(REPO_ROOT)).returncode
        if rc != 0:
            return rc

    print("[db_migrate] alembic upgrade head", flush=True)
    return subprocess.run(["alembic", "upgrade", "head"], cwd=str(REPO_ROOT)).returncode


if __name__ == "__main__":
    sys.exit(main())
