"""Coverage for the login-history/session-info additions to
backend/routers/auth.py:

- LoginHistory (models.py) gets one row per POST /api/auth/login attempt, success
  or failure, written by _record_login_attempt().
- GET /api/auth/login-history is a paginated, strictly-self-scoped view of that
  history (frontend/profile.html's security tab).
- GET /api/auth/current-session describes only the caller's OWN token (IP/device/
  issued/expiry) — there is no per-device session store in this app, so this is
  deliberately NOT a list of other active sessions.
- _maybe_notify_new_device() sends a best-effort email the first time a successful
  login is seen from an IP not present in any of the account's prior successful
  logins (and never on the account's very first successful login ever).
- DELETE /api/profile/me (backend/routers/profile.py) clears LoginHistory rows for
  the deleted account, same as it already does for PasswordReset/Message/etc.

Under the httpx ASGITransport test client, request.client.host is always
"127.0.0.1" (verified directly) — tests that need to simulate "a different IP"
seed a LoginHistory row with a different ip_address directly via db_session rather
than trying to vary the test client's own address.
"""

import pyotp
import pytest
from sqlalchemy import select

from backend.auth import get_password_hash, COOKIE_NAME
from backend.models import LoginHistory, User

pytestmark = pytest.mark.asyncio

TEST_CLIENT_IP = "127.0.0.1"


async def _make_user(db_session, username="loginhistuser", password="password123", **kwargs):
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash(password),
        role="user",
        **kwargs,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ─── LoginHistory rows written on every attempt ────────────────────────────────

async def test_successful_login_records_history_row(client, db_session):
    user = await _make_user(db_session)
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    assert r.status_code == 200

    rows = (await db_session.execute(select(LoginHistory).where(LoginHistory.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is True
    assert rows[0].failure_reason is None
    assert rows[0].ip_address == TEST_CLIENT_IP
    assert rows[0].username_attempted == user.username


async def test_wrong_password_records_failed_history_row_with_reason(client, db_session):
    user = await _make_user(db_session)
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "wrong-password"})
    assert r.status_code == 401

    rows = (await db_session.execute(select(LoginHistory).where(LoginHistory.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].failure_reason == "invalid_credentials"


async def test_unknown_username_records_history_row_with_null_user_id(client, db_session):
    r = await client.post("/api/auth/login", json={"username": "nobody-at-all", "password": "whatever123"})
    assert r.status_code == 401

    rows = (await db_session.execute(
        select(LoginHistory).where(LoginHistory.username_attempted == "nobody-at-all")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert rows[0].success is False
    assert rows[0].failure_reason == "invalid_credentials"


async def test_inactive_account_records_history_row_with_reason(client, db_session):
    user = await _make_user(db_session, username="inactiveuser", is_active=False)
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    assert r.status_code == 403

    rows = (await db_session.execute(select(LoginHistory).where(LoginHistory.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].failure_reason == "account_inactive"


async def test_missing_totp_code_records_totp_required_reason(client, db_session):
    secret = pyotp.random_base32()
    user = await _make_user(db_session, username="totphistuser", totp_enabled=True, totp_secret=secret)
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    assert r.status_code == 401

    rows = (await db_session.execute(select(LoginHistory).where(LoginHistory.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].failure_reason == "totp_required"


async def test_wrong_totp_code_records_invalid_totp_reason(client, db_session):
    secret = pyotp.random_base32()
    user = await _make_user(db_session, username="totphistuser2", totp_enabled=True, totp_secret=secret)
    r = await client.post("/api/auth/login", json={
        "username": user.username, "password": "password123", "totp_code": "000000",
    })
    assert r.status_code == 401

    rows = (await db_session.execute(select(LoginHistory).where(LoginHistory.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].failure_reason == "invalid_totp"


# ─── GET /api/auth/login-history ───────────────────────────────────────────────

async def test_login_history_endpoint_requires_auth(client, db_session):
    r = await client.get("/api/auth/login-history")
    assert r.status_code == 401


async def test_login_history_endpoint_returns_own_entries_only(client, db_session):
    user_a = await _make_user(db_session, username="usera")
    user_b = await _make_user(db_session, username="userb")
    db_session.add_all([
        LoginHistory(user_id=user_a.id, username_attempted="usera", success=True, ip_address="1.1.1.1"),
        LoginHistory(user_id=user_b.id, username_attempted="userb", success=True, ip_address="2.2.2.2"),
    ])
    await db_session.commit()

    r = await client.post("/api/auth/login", json={"username": "usera", "password": "password123"})
    token = r.json()["access_token"]

    res = await client.get("/api/auth/login-history", headers=_bearer(token))
    assert res.status_code == 200
    data = res.json()
    # The 1 seeded row + this test's own successful login = 2, never userb's.
    assert data["total"] == 2
    assert all(item["ip_address"] != "2.2.2.2" for item in data["items"])


async def test_login_history_endpoint_is_paginated(client, db_session):
    user = await _make_user(db_session, username="paginateduser")
    db_session.add_all([
        LoginHistory(user_id=user.id, username_attempted=user.username, success=True, ip_address=f"10.0.0.{i}")
        for i in range(25)
    ])
    await db_session.commit()
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    token = r.json()["access_token"]

    page1 = (await client.get("/api/auth/login-history?limit=10&offset=0", headers=_bearer(token))).json()
    assert len(page1["items"]) == 10
    assert page1["total"] == 26  # 25 seeded + this test's own login

    page2 = (await client.get("/api/auth/login-history?limit=10&offset=10", headers=_bearer(token))).json()
    assert len(page2["items"]) == 10

    # limit is clamped, not rejected, for an out-of-range request
    clamped = (await client.get("/api/auth/login-history?limit=999&offset=0", headers=_bearer(token))).json()
    assert len(clamped["items"]) <= 50


# ─── GET /api/auth/current-session ──────────────────────────────────────────────

async def test_current_session_requires_auth(client, db_session):
    r = await client.get("/api/auth/current-session")
    assert r.status_code == 401


async def test_current_session_describes_this_token(client, db_session):
    user = await _make_user(db_session, username="sessioninfouser")
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    token = r.json()["access_token"]

    res = await client.get("/api/auth/current-session", headers=_bearer(token))
    assert res.status_code == 200
    data = res.json()
    assert data["ip_address"] == TEST_CLIENT_IP
    assert data["issued_at"] is not None
    assert data["expires_at"] is not None
    # issued_at must be strictly before expires_at
    assert data["issued_at"] < data["expires_at"]


# ─── New-device email (best-effort, built on LoginHistory) ─────────────────────

async def test_no_new_device_email_on_first_ever_login(client, db_session, monkeypatch):
    """The account's very first successful login has no prior history to compare
    against — this must be treated as "welcome", not "new device"."""
    import backend.routers.auth as auth_router
    calls = []

    async def _fake_send(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(auth_router, "_send_notification_email", _fake_send)
    user = await _make_user(db_session, username="freshuser")
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    assert r.status_code == 200
    assert calls == []


async def test_new_device_email_sent_for_unseen_ip(client, db_session, monkeypatch):
    """A prior successful login exists from a DIFFERENT ip_address (seeded directly,
    since the test client's own IP is always 127.0.0.1 — see module docstring) — the
    next successful login must be treated as a new device and trigger the email."""
    import backend.routers.auth as auth_router
    calls = []

    async def _fake_send(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(auth_router, "_send_notification_email", _fake_send)
    user = await _make_user(db_session, username="returninguser")
    db_session.add(LoginHistory(
        user_id=user.id, username_attempted=user.username, success=True, ip_address="9.9.9.9",
    ))
    await db_session.commit()

    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == user.email


async def test_no_new_device_email_for_already_seen_ip(client, db_session, monkeypatch):
    """A prior successful login already exists from THIS SAME ip_address
    (127.0.0.1, matching the test client) — must NOT be treated as a new device."""
    import backend.routers.auth as auth_router
    calls = []

    async def _fake_send(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(auth_router, "_send_notification_email", _fake_send)
    user = await _make_user(db_session, username="knowndeviceuser")
    db_session.add(LoginHistory(
        user_id=user.id, username_attempted=user.username, success=True, ip_address=TEST_CLIENT_IP,
    ))
    await db_session.commit()

    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    assert r.status_code == 200
    assert calls == []


async def test_new_device_email_failure_never_breaks_login(client, db_session, monkeypatch):
    """_maybe_notify_new_device must never propagate an exception into the login
    response — a broken/misconfigured email path is not a reason to lock users out."""
    import backend.routers.auth as auth_router

    async def _boom(*args, **kwargs):
        raise RuntimeError("SMTP exploded")

    monkeypatch.setattr(auth_router, "_send_notification_email", _boom)
    user = await _make_user(db_session, username="boomuser")
    db_session.add(LoginHistory(
        user_id=user.id, username_attempted=user.username, success=True, ip_address="8.8.8.8",
    ))
    await db_session.commit()

    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    assert r.status_code == 200
    assert COOKIE_NAME in r.cookies


# ─── Account deletion clears LoginHistory ───────────────────────────────────────

async def test_deleting_account_clears_login_history(client, db_session):
    user = await _make_user(db_session, username="deleteme")
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "password123"})
    token = r.json()["access_token"]
    rows_before = (await db_session.execute(select(LoginHistory).where(LoginHistory.user_id == user.id))).scalars().all()
    assert len(rows_before) >= 1

    res = await client.request(
        "DELETE", "/api/profile/me", headers=_bearer(token), json={"password": "password123"},
    )
    assert res.status_code == 204

    rows_after = (await db_session.execute(select(LoginHistory).where(LoginHistory.user_id == user.id))).scalars().all()
    assert rows_after == []
