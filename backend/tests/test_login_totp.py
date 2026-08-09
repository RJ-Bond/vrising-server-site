"""Coverage for the POST /api/auth/login contract when a user has 2FA (TOTP)
enabled — see backend/routers/auth.py `login()`. This is the exact behavior
frontend/login.html and frontend/maintenance.html's admin-bypass form now
depend on (both were missing any TOTP input before this fix, permanently
locking out any user who enabled 2FA via profile.html)."""

import pyotp
import pytest

from backend.auth import get_password_hash, COOKIE_NAME
from backend.models import User

pytestmark = pytest.mark.asyncio

TOTP_REQUIRED_DETAIL = "Требуется код 2FA"


async def _make_totp_user(db_session, username="totpuser", password="correct-horse"):
    secret = pyotp.random_base32()
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash(password),
        role="user",
        totp_enabled=True,
        totp_secret=secret,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user, secret


async def test_login_without_totp_code_is_rejected(client, db_session):
    user, _secret = await _make_totp_user(db_session)
    res = await client.post("/api/auth/login", json={
        "username": user.username, "password": "correct-horse",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == TOTP_REQUIRED_DETAIL


async def test_login_with_wrong_totp_code_is_rejected(client, db_session):
    user, _secret = await _make_totp_user(db_session)
    res = await client.post("/api/auth/login", json={
        "username": user.username, "password": "correct-horse", "totp_code": "000000",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == TOTP_REQUIRED_DETAIL


async def test_login_with_correct_totp_code_succeeds(client, db_session):
    user, secret = await _make_totp_user(db_session)
    code = pyotp.TOTP(secret).now()
    res = await client.post("/api/auth/login", json={
        "username": user.username, "password": "correct-horse", "totp_code": code,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["user"]["username"] == user.username
    # httpOnly cookie set by the server (frontend relies on this, not the body,
    # for the actual session — see _set_auth_cookie in backend/helpers.py)
    assert COOKIE_NAME in res.cookies


async def test_login_wrong_password_takes_priority_over_totp_prompt(client, db_session):
    """A wrong password must still fail as 'Invalid credentials', not leak that
    the account has 2FA enabled by asking for a code first."""
    user, _secret = await _make_totp_user(db_session)
    res = await client.post("/api/auth/login", json={
        "username": user.username, "password": "wrong-password",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid credentials"
