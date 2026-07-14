"""Regression tests for the security-review fixes: session revocation on password
change, and refusing to trust a client-asserted identity in /api/online/ping."""
import asyncio
from datetime import datetime, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, password="correct horse battery staple", role="user"):
    user = User(username=username, email=f"{username}@example.com", hashed_password=get_password_hash(password), role=role)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_change_password_revokes_old_token_but_new_token_still_works(client, db_session):
    user = await _make_user(db_session, "alice", password="old-password-123")
    # JWT `iat` here is whole-second (NumericDate) and the token has no jti/nonce, so
    # two tokens minted in the same wall-clock second for the same user are literally
    # identical strings. Sleep past the second boundary so "old_token" genuinely
    # predates the request, like a real separate prior session would.
    old_token = create_access_token({"sub": str(user.id)})
    await asyncio.sleep(1.1)

    r = await client.post(
        "/api/auth/change-password",
        json={"old_password": "old-password-123", "new_password": "new-password-456"},
        headers=_bearer(old_token),
    )
    assert r.status_code == 200
    new_token = r.json()["access_token"]
    assert new_token and new_token != old_token

    # The token used to MAKE the change-password request must now be rejected —
    # this is the whole point: a stolen/leaked old token stops working.
    r_old = await client.get("/api/auth/me", headers=_bearer(old_token))
    assert r_old.status_code == 401

    # But the freshly-issued token from the response keeps working, so the user
    # who legitimately just changed their own password isn't logged out.
    r_new = await client.get("/api/auth/me", headers=_bearer(new_token))
    assert r_new.status_code == 200
    assert r_new.json()["username"] == "alice"


async def test_online_ping_cannot_spoof_another_users_identity(client, db_session):
    victim = await _make_user(db_session, "victim")
    attacker_token = create_access_token({"sub": str((await _make_user(db_session, "attacker")).id)})

    # Attacker is authenticated as THEMSELVES, but claims to be "victim" in the body.
    r = await client.post(
        "/api/online/ping",
        json={"visitor_id": "spoof-attempt", "is_authed": True, "username": "victim", "page": "Сайт"},
        headers=_bearer(attacker_token),
    )
    assert r.status_code == 204

    await db_session.refresh(victim)
    assert victim.last_active_at is None, "victim's last_active_at must not move from an attacker's spoofed claim"


async def test_online_ping_updates_last_active_for_the_real_authenticated_user(client, db_session):
    user = await _make_user(db_session, "bob")
    token = create_access_token({"sub": str(user.id)})

    r = await client.post(
        "/api/online/ping",
        json={"visitor_id": "real-session", "is_authed": True, "username": "bob", "page": "Сайт"},
        headers=_bearer(token),
    )
    assert r.status_code == 204

    await db_session.refresh(user)
    assert user.last_active_at is not None
    assert (datetime.now(timezone.utc) - user.last_active_at.replace(tzinfo=timezone.utc)).total_seconds() < 30


async def test_online_ping_anonymous_request_is_not_treated_as_authed(client, db_session):
    # No Authorization header at all — a fully anonymous visitor claiming is_authed.
    r = await client.post(
        "/api/online/ping",
        json={"visitor_id": "anon-spoof", "is_authed": True, "username": "nobody", "page": "Сайт"},
    )
    assert r.status_code == 204  # anonymous pings are still allowed, just not trusted for identity
