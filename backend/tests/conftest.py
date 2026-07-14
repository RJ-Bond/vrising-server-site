import os
import sys
from pathlib import Path

# backend/database.py reads DATABASE_URL at import time, so this must run before
# any backend module is imported anywhere in the test session — conftest.py is
# collected first, which is why this lives here rather than in a fixture.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker  # noqa: E402
from backend.models import Base  # noqa: E402


@pytest_asyncio.fixture
async def db_engine(tmp_path, monkeypatch):
    """A fresh file-based sqlite DB per test, wired up as backend.database's engine.

    File-based (not :memory:) because SQLAlchemy's async pool can open more than one
    connection, and each connection to sqlite ':memory:' is its own separate empty
    database — tables created on connection #1 wouldn't exist on connection #2.
    """
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, echo=False)
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    import backend.database as database
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "AsyncSessionLocal", session_local)
    # main.py imports `engine` by name (used directly as `AsyncSession(engine, ...)`
    # in background tasks) — patch that reference too. It does NOT import
    # AsyncSessionLocal; the request path (Depends(get_db)) reads database.py's
    # module global at call time, so patching database.AsyncSessionLocal above
    # already covers it.
    import backend.main as main
    monkeypatch.setattr(main, "engine", engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    import backend.database as database
    async with database.AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine):
    import httpx
    from backend.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
