"""backend/database.py's _set_sqlite_pragmas — the connect-time WAL/synchronous PRAGMA
setup. The many concurrent asyncio background tasks (game-plugin heartbeat, playtime
updates, online-ping, monitor polling, ... — see backend/main.py's `_*_task` functions)
all write to one SQLite file; WAL mode is what keeps a slow writer from locking out every
reader/other writer at once (SQLite's default rollback-journal mode takes an exclusive
lock for the whole write transaction).

Doesn't use the conftest `db_engine` fixture on purpose: that fixture builds its own
`create_async_engine(...)` instance, and SQLAlchemy's `connect` event is registered
against one *specific* Engine object (backend.database.engine, at import time) — a
different Engine instance never fires it. conftest.py also defaults DATABASE_URL to
`sqlite+aiosqlite:///:memory:` for the whole test session, and SQLite explicitly doesn't
support WAL for in-memory databases (silently stays "memory" mode) — so testing this
meaningfully needs its own real, file-based engine with the same pragma function attached
by hand, exactly like backend/database.py attaches it to the real one.
"""
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.database import _set_sqlite_pragmas


@pytest.mark.asyncio
async def test_wal_and_synchronous_pragmas_applied_on_connect(tmp_path):
    db_path = tmp_path / "wal_check.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", echo=False)
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    try:
        async with engine.connect() as conn:
            journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            synchronous = (await conn.execute(text("PRAGMA synchronous"))).scalar()
        assert journal_mode.lower() == "wal"
        assert synchronous == 1  # NORMAL (SQLite's numeric pragma value: 0=OFF,1=NORMAL,2=FULL)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wal_mode_persists_in_db_file_across_new_connections(tmp_path):
    """journal_mode=WAL is stored in the database file itself (unlike `synchronous`,
    which is per-connection) — a second, freshly-opened engine against the same file
    should already report WAL even before _set_sqlite_pragmas runs again."""
    db_path = tmp_path / "wal_persist.db"
    engine1 = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", echo=False)
    event.listen(engine1.sync_engine, "connect", _set_sqlite_pragmas)
    async with engine1.connect() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        await conn.commit()
    await engine1.dispose()

    # Plain second engine, deliberately WITHOUT re-attaching the pragma listener.
    engine2 = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", echo=False)
    try:
        async with engine2.connect() as conn:
            journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        assert journal_mode.lower() == "wal"
    finally:
        await engine2.dispose()
