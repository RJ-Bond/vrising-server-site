import os
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:////data/vrising.db")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """Multiple independent asyncio background tasks (game-plugin heartbeat,
        playtime updates, online-ping, monitor polling, scheduled publish, cleanup,
        ... — see the `_*_task` functions in backend/main.py) each open their own
        AsyncSession against this one SQLite file concurrently. SQLite's default
        journal mode (rollback journal / DELETE) takes an exclusive lock for the
        whole duration of any write transaction, blocking every other reader AND
        writer at once — a real contention risk with this many independent writers.

        WAL (write-ahead log) instead lets readers keep reading a consistent
        last-committed snapshot while a writer is mid-transaction, and lets writers
        queue behind each other instead of a reader/writer collision immediately
        raising "database is locked" — the concurrency model this app actually
        needs. `synchronous=NORMAL` is the pragma SQLite's own docs recommend
        pairing with WAL specifically: still fsyncs at WAL-checkpoint boundaries
        (safe against an app crash or OS crash), just not after every single
        commit the way FULL does — only a hard power-loss between checkpoints could
        roll back the most recent commits, never corrupt the database file itself.

        Fires once per pooled DBAPI connection (SQLAlchemy's `connect` event, bound
        to the async engine's underlying sync_engine — the documented way to run
        connection-time PRAGMAs against an async SQLAlchemy engine). journal_mode is
        persisted in the database file itself once set, so this is a fast no-op
        check on every connection after the very first one ever made against a given
        file; `synchronous` is a per-connection setting and does need reasserting
        every time.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
