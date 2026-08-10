"""Coverage for the role-aware access-token/cookie expiry added in backend/auth.py
(ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES, access_token_expire_minutes_for_role,
create_access_token_for_user) — see CLAUDE.md's admin-role section and item 2 of the
security/admin-tooling pass this shipped as part of.

admin/superadmin tokens (and their Set-Cookie max-age) get a 12-hour lifetime instead
of the normal-user/moderator 7-day one: those two tiers hold role management, backups,
deploy/SSL, and RCON access, so a leaked or forgotten-open session for one of them
should self-expire within a work day. Deliberately NOT applied to moderator (content/
user moderation only — a blast radius much closer to a normal user's).

Covers both the unit-level helper and the full request path (register/login/
change-password/logout-everywhere), since the whole point of routing every
token-issuing call site through create_access_token_for_user()/_set_auth_cookie(...,
role) is that none of them can silently regress back to the flat 7-day duration."""
import asyncio

import pytest
from jose import jwt

from backend.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
    access_token_expire_minutes_for_role,
    create_access_token_for_user,
    get_password_hash,
)
from backend.models import User

pytestmark = pytest.mark.asyncio


def _make_user_obj(user_id, role):
    """A throwaway, un-persisted User for the pure-function unit tests below —
    create_access_token_for_user only reads .id and .role."""
    u = User(id=user_id, username=f"u{user_id}", email=f"u{user_id}@example.com",
              hashed_password="x", role=role)
    return u


async def _make_user(db_session, username, role="user", password="password1"):
    user = User(username=username, email=f"{username.lower()}@example.com",
                hashed_password=get_password_hash(password), role=role)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _decode_exp_iat(token):
    claims = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    return claims["iat"], claims["exp"]


def _set_cookie_header(response):
    cookies = response.headers.get_list("set-cookie")
    return next(c for c in cookies if c.startswith("vrising_token="))


# ── 1. access_token_expire_minutes_for_role() ───────────────────────────────

@pytest.mark.parametrize("role,expected", [
    ("user", ACCESS_TOKEN_EXPIRE_MINUTES),
    ("moderator", ACCESS_TOKEN_EXPIRE_MINUTES),
    ("admin", ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES),
    ("superadmin", ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES),
])
async def test_expire_minutes_for_role(role, expected):
    assert access_token_expire_minutes_for_role(role) == expected


async def test_admin_duration_is_shorter_than_normal_user_duration():
    # The whole point of this feature: assert the ordering, not just the raw numbers,
    # so a future edit that accidentally makes them equal (or inverts them) fails
    # loudly here instead of just quietly widening the admin blast-radius window.
    assert ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES < ACCESS_TOKEN_EXPIRE_MINUTES


async def test_unrecognized_role_gets_normal_user_duration():
    assert access_token_expire_minutes_for_role("garbled-nonsense") == ACCESS_TOKEN_EXPIRE_MINUTES


# ── 2. create_access_token_for_user() ───────────────────────────────────────

@pytest.mark.parametrize("role,expected_minutes", [
    ("user", ACCESS_TOKEN_EXPIRE_MINUTES),
    ("moderator", ACCESS_TOKEN_EXPIRE_MINUTES),
    ("admin", ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES),
    ("superadmin", ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES),
])
async def test_create_access_token_for_user_bakes_in_role_duration(role, expected_minutes):
    user = _make_user_obj(1, role)
    token = create_access_token_for_user(user)
    iat, exp = _decode_exp_iat(token)
    assert exp - iat == expected_minutes * 60


# ── 3. Full request path: register / login ──────────────────────────────────

async def test_register_issues_normal_user_duration_token(client, db_session):
    r = await client.post("/api/auth/register", json={
        "username": "TokenExpiryUser",
        "email": "tokenexpiryuser@example.com",
        "password": "letmein123",
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    iat, exp = _decode_exp_iat(token)
    assert exp - iat == ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert f"Max-Age={ACCESS_TOKEN_EXPIRE_MINUTES * 60}" in _set_cookie_header(r)


async def test_login_as_admin_issues_short_lived_token_and_cookie(client, db_session):
    await _make_user(db_session, "TokenExpiryAdmin", role="admin", password="password1")
    r = await client.post("/api/auth/login", json={"username": "TokenExpiryAdmin", "password": "password1"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    iat, exp = _decode_exp_iat(token)
    assert exp - iat == ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert f"Max-Age={ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60}" in _set_cookie_header(r)


async def test_login_as_superadmin_issues_short_lived_token(client, db_session):
    await _make_user(db_session, "TokenExpirySuperadmin", role="superadmin", password="password1")
    r = await client.post("/api/auth/login", json={"username": "TokenExpirySuperadmin", "password": "password1"})
    assert r.status_code == 200, r.text
    iat, exp = _decode_exp_iat(r.json()["access_token"])
    assert exp - iat == ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60


async def test_login_as_moderator_issues_normal_user_duration_token(client, db_session):
    # Moderator is deliberately excluded from the tightened duration — content/user
    # moderation only, not infra access. Regression guard against the cutoff drifting
    # to role_level >= 1 (moderator) instead of the intended >= 2 (admin).
    await _make_user(db_session, "TokenExpiryMod", role="moderator", password="password1")
    r = await client.post("/api/auth/login", json={"username": "TokenExpiryMod", "password": "password1"})
    assert r.status_code == 200, r.text
    iat, exp = _decode_exp_iat(r.json()["access_token"])
    assert exp - iat == ACCESS_TOKEN_EXPIRE_MINUTES * 60


async def test_login_as_normal_user_issues_normal_duration_token(client, db_session):
    await _make_user(db_session, "TokenExpiryNormal", role="user", password="password1")
    r = await client.post("/api/auth/login", json={"username": "TokenExpiryNormal", "password": "password1"})
    assert r.status_code == 200, r.text
    iat, exp = _decode_exp_iat(r.json()["access_token"])
    assert exp - iat == ACCESS_TOKEN_EXPIRE_MINUTES * 60


# ── 4. Reissue paths keep the role-aware duration ───────────────────────────

async def test_change_password_reissue_keeps_admin_short_duration(client, db_session):
    user = await _make_user(db_session, "TokenExpiryChangePw", role="admin", password="old-password-123")
    old_token = create_access_token_for_user(user)
    await asyncio.sleep(1.1)  # see test_security_fixes.py: iat is whole-second, must genuinely predate the request

    r = await client.post(
        "/api/auth/change-password",
        json={"old_password": "old-password-123", "new_password": "new-password-456"},
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert r.status_code == 200, r.text
    iat, exp = _decode_exp_iat(r.json()["access_token"])
    assert exp - iat == ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60


async def test_logout_everywhere_reissue_keeps_superadmin_short_duration(client, db_session):
    user = await _make_user(db_session, "TokenExpiryLogoutEverywhere", role="superadmin")
    old_token = create_access_token_for_user(user)
    await asyncio.sleep(1.1)

    r = await client.post("/api/auth/logout-everywhere", headers={"Authorization": f"Bearer {old_token}"})
    assert r.status_code == 200, r.text
    iat, exp = _decode_exp_iat(r.json()["access_token"])
    assert exp - iat == ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60


# ── 5. Setup (first superadmin account) ──────────────────────────────────────

async def test_setup_complete_issues_short_lived_superadmin_token(client, db_session):
    r = await client.post("/api/setup/complete", json={
        "username": "FoundingSuperadmin",
        "email": "foundingsuperadmin@example.com",
        "password": "letmein123",
    })
    assert r.status_code == 201, r.text
    assert r.json()["user"]["role"] == "superadmin"
    iat, exp = _decode_exp_iat(r.json()["access_token"])
    assert exp - iat == ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES * 60
