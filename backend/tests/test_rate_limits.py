"""Regression tests confirming dedicated @limiter.limit decorators are actually wired
on endpoints that previously relied only on the generous global default_limits
(200/minute, backend/rate_limit.py) — POST /api/auth/2fa/enable, POST
/api/auth/2fa/disable (credential-sensitive, brute-forceable 6-digit codes, same
5/minute tier as change-password/change-email/register in the same file), and GET
/api/search (unauthenticated, `min_length=1` so single-character/leading-wildcard
queries are cheap to fire on every keystroke against a potentially large table;
30/minute).

Each test doesn't need a *valid* request to prove the limiter is wired — slowapi's
@limiter.limit decorator counts every call before the handler body runs, so a request
that 400s on its own merits (e.g. no pending TOTP secret) still consumes one unit of
quota. Sending N+1 requests and asserting the last one is 429 is enough.

limiter.reset() is autoused per test via conftest.py's `_reset_rate_limiter` fixture,
so no manual reset is needed here despite each test issuing many requests."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def test_2fa_enable_rate_limited_at_5_per_minute(client, db_session):
    user = await _make_user(db_session, "RateLimit2faEnable")
    headers = _bearer(user)
    statuses = []
    for _ in range(6):
        r = await client.post("/api/auth/2fa/enable", json={"code": "000000"}, headers=headers)
        statuses.append(r.status_code)
    # First 5 hit the handler (and 400 — no pending secret from /2fa/setup); the 6th
    # is rejected by the limiter itself.
    assert statuses[:5] == [400] * 5
    assert statuses[5] == 429


async def test_2fa_disable_rate_limited_at_5_per_minute(client, db_session):
    user = await _make_user(db_session, "RateLimit2faDisable")
    headers = _bearer(user)
    statuses = []
    for _ in range(6):
        r = await client.post("/api/auth/2fa/disable", json={"code": "000000"}, headers=headers)
        statuses.append(r.status_code)
    # First 5 hit the handler (and 400 — 2FA isn't enabled); the 6th is rate-limited.
    assert statuses[:5] == [400] * 5
    assert statuses[5] == 429


async def test_search_rate_limited_at_30_per_minute(client, db_session):
    statuses = []
    for _ in range(31):
        r = await client.get("/api/search", params={"q": "x"})
        statuses.append(r.status_code)
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429
