import os
import sys
from pathlib import Path

# backend/database.py reads DATABASE_URL at import time, so this must run before
# any backend module is imported anywhere in the test session — conftest.py is
# collected first, which is why this lives here rather than in a fixture.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
# backend/auth.py refuses to start if SECRET_KEY is still the insecure placeholder
# default — same "must run before any backend module import" reasoning as above.
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-use")
# backend/main.py refuses to start with ALLOWED_ORIGINS unset (wildcard + credentials
# can't work in any real browser) — same reasoning.
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker  # noqa: E402
from backend.models import Base  # noqa: E402
from backend.rate_limit import limiter  # noqa: E402
from backend.helpers import _failed_totp_attempts, _failed_login_attempts  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """slowapi's Limiter keys by client IP (get_remote_address) in an in-memory store
    that's process-global, not per-test — every test client request in this same
    pytest run shares one IP, so without this a test late in the suite can fail from a
    quota an earlier, unrelated test already spent (see default_limits / @limiter.limit
    added across backend/routers/*.py and rate_limit.py)."""
    limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _reset_totp_bruteforce_state():
    """_failed_totp_attempts (backend/helpers.py, backing _totp_attempts_exceeded/
    _record_failed_totp/_reset_failed_totp) is a process-global dict keyed by
    user_id — same "not reset between tests" hazard _reset_rate_limiter above
    already handles for slowapi's Limiter, just for a second in-memory store.
    Each test gets its own fresh file-based sqlite DB (db_engine fixture below), so
    autoincrement IDs restart at 1 every time — a test late in the full suite can
    easily create a user with the SAME id an earlier, unrelated TOTP-brute-force
    test (e.g. test_login_totp_bruteforce.py, or the deliberately-failing-TOTP
    cases in test_login_history.py) already recorded 5 failed attempts against.
    Without this reset, that later test's otherwise-correct TOTP code gets
    rejected with "too many attempts" instead of succeeding — reproduced via
    backend/tests/test_login_totp.py::test_login_with_correct_totp_code_succeeds
    passing standalone but failing in a full `pytest backend/tests/` run."""
    _failed_totp_attempts.clear()
    yield
    _failed_totp_attempts.clear()


@pytest.fixture(autouse=True)
def _reset_login_bruteforce_state():
    """Same hazard as _reset_totp_bruteforce_state above, for the newer
    _failed_login_attempts dict (backing _login_attempts_exceeded/
    _record_failed_login/_reset_failed_login) — process-global, keyed by
    username this time rather than user_id, but the same "leaks across tests
    that happen to reuse a name/id" risk applies."""
    _failed_login_attempts.clear()
    yield
    _failed_login_attempts.clear()


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
