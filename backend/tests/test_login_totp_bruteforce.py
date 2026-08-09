"""Per-account TOTP brute-force guard on POST /api/auth/login (backend/routers/auth.py),
independent of that endpoint's existing @limiter.limit("10/minute") — the global limit
is shared across every failure mode (wrong password included) from any caller, so it
doesn't specifically stop an attacker who already has the password from grinding
through 6-digit TOTP codes for one known account. Tracking lives in
backend/helpers.py's _failed_totp_attempts (in-memory, 5 failures / 5 minutes)."""

import pyotp
import pytest

from backend.auth import get_password_hash
from backend.helpers import _failed_totp_attempts
from backend.models import User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_totp_attempts():
    """_failed_totp_attempts is a process-global in-memory dict (same as slowapi's
    limiter state) — clear it before/after each test so runs don't leak into each
    other regardless of execution order."""
    _failed_totp_attempts.clear()
    yield
    _failed_totp_attempts.clear()


async def _make_totp_user(db_session, username):
    secret = pyotp.random_base32()
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        totp_enabled=True,
        totp_secret=secret,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user, secret


async def test_totp_blocked_after_5_failed_attempts_even_with_correct_code(client, db_session):
    user, secret = await _make_totp_user(db_session, "TotpBruteVictim")
    correct_code = pyotp.TOTP(secret).now()

    statuses = []
    for _ in range(5):
        r = await client.post("/api/auth/login", json={
            "username": user.username, "password": "password1", "totp_code": "000000",
        })
        statuses.append(r.status_code)
    assert statuses == [401] * 5

    # 6th attempt, this time with the actually-correct code — must still be rejected,
    # proving the block is on the account/attempt-count, not on the code itself.
    r = await client.post("/api/auth/login", json={
        "username": user.username, "password": "password1", "totp_code": correct_code,
    })
    assert r.status_code == 401


async def test_totp_attempts_scoped_per_account(client, db_session):
    victim, _ = await _make_totp_user(db_session, "TotpBruteVictim2")
    other, other_secret = await _make_totp_user(db_session, "TotpBruteBystander")

    for _ in range(5):
        r = await client.post("/api/auth/login", json={
            "username": victim.username, "password": "password1", "totp_code": "000000",
        })
        assert r.status_code == 401

    # A different account's correct code must still work — the guard is per-user_id,
    # not global.
    other_code = pyotp.TOTP(other_secret).now()
    r = await client.post("/api/auth/login", json={
        "username": other.username, "password": "password1", "totp_code": other_code,
    })
    assert r.status_code == 200


async def test_successful_totp_login_resets_attempt_counter(client, db_session):
    user, secret = await _make_totp_user(db_session, "TotpBruteRecovers")

    for _ in range(3):
        r = await client.post("/api/auth/login", json={
            "username": user.username, "password": "password1", "totp_code": "000000",
        })
        assert r.status_code == 401

    correct_code = pyotp.TOTP(secret).now()
    r = await client.post("/api/auth/login", json={
        "username": user.username, "password": "password1", "totp_code": correct_code,
    })
    assert r.status_code == 200
    assert user.id not in _failed_totp_attempts
