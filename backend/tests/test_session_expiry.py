"""Regression coverage for GET /api/auth/session-expiry — the small companion to
GET /api/auth/me added for admin.html's dashboard session-expiry warning (see
CLAUDE.md / backend/routers/auth.py's docstring on it). Admin/superadmin tokens now
expire after ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES (12h, tightened from the old flat 7-day
duration — see test_admin_token_expiry.py) instead of the normal 7 days, so the admin
panel needs a way to warn before the session dies mid-edit without ever persisting the
raw JWT client-side (login.html deliberately never stores access_token — see its own
comment)."""
from datetime import datetime, timezone

import pytest
from jose import jwt

from backend.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
    create_access_token_for_user,
    get_password_hash,
)
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(
        username=username, email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"), role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_requires_auth(client, db_session):
    r = await client.get("/api/auth/session-expiry")
    assert r.status_code == 401


async def test_returns_the_tokens_own_exp_claim(client, db_session):
    user = await _make_user(db_session, "SessExpiryUser")
    token = create_access_token_for_user(user)
    r = await client.get("/api/auth/session-expiry", headers=_bearer(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_at"] is not None

    expected_exp = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["exp"]
    actual = datetime.fromisoformat(body["expires_at"])
    assert actual == datetime.fromtimestamp(expected_exp, tz=timezone.utc)


async def test_admin_token_reports_shorter_window_than_normal_user(client, db_session):
    admin = await _make_user(db_session, "SessExpiryAdmin", role="admin")
    normal = await _make_user(db_session, "SessExpiryNormal", role="user")
    admin_token = create_access_token_for_user(admin)
    normal_token = create_access_token_for_user(normal)

    r_admin = await client.get("/api/auth/session-expiry", headers=_bearer(admin_token))
    r_normal = await client.get("/api/auth/session-expiry", headers=_bearer(normal_token))
    assert r_admin.status_code == 200 and r_normal.status_code == 200

    now = datetime.now(timezone.utc)
    admin_remaining = (datetime.fromisoformat(r_admin.json()["expires_at"]) - now).total_seconds()
    normal_remaining = (datetime.fromisoformat(r_normal.json()["expires_at"]) - now).total_seconds()
    # Loose bounds (not exact-second equality — wall-clock drift between token
    # issuance above and "now" here) — just needs to land near its own role duration
    # and clearly under the other role's.
    assert ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60 - 30 < admin_remaining < ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert ACCESS_TOKEN_EXPIRE_MINUTES * 60 - 30 < normal_remaining < ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert admin_remaining < normal_remaining


async def test_invalid_token_returns_401(client, db_session):
    r = await client.get("/api/auth/session-expiry", headers=_bearer("not-a-real-token"))
    assert r.status_code == 401


async def test_works_via_cookie_like_get_current_user(client, db_session):
    # login sets the httpOnly cookie via _set_auth_cookie — exercise the real path
    # rather than only the Bearer-header branch above.
    await _make_user(db_session, "SessExpiryCookie", role="admin")
    login = await client.post("/api/auth/login", json={"username": "SessExpiryCookie", "password": "password1"})
    assert login.status_code == 200, login.text
    r = await client.get("/api/auth/session-expiry")
    assert r.status_code == 200, r.text
    assert r.json()["expires_at"] is not None
