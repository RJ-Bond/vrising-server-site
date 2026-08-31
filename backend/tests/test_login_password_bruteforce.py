"""Per-account brute-force guard on POST /api/auth/login (backend/routers/auth.py),
independent of that endpoint's existing @limiter.limit("10/minute") — that quota is
per source IP, so a distributed/low-and-slow attack rotating IPs against one known
username stays under it indefinitely. Tracking lives in backend/helpers.py's
_failed_login_attempts (in-memory, keyed by username, 5 failures / 5 minutes — same
pattern and window as the existing per-account TOTP guard in
test_login_totp_bruteforce.py)."""

import pytest

from backend.auth import get_password_hash
from backend.helpers import _failed_login_attempts
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("correctpass1"),
        role="user",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_login_blocked_after_5_failed_attempts_even_with_correct_password(client, db_session):
    user = await _make_user(db_session, "LoginBruteVictim")

    statuses = []
    for _ in range(5):
        r = await client.post("/api/auth/login", json={"username": user.username, "password": "wrongpass"})
        statuses.append(r.status_code)
    assert statuses == [401] * 5

    # 6th attempt, this time with the actually-correct password — must still be
    # rejected, proving the block is on the account/attempt-count, not the password.
    r = await client.post("/api/auth/login", json={"username": user.username, "password": "correctpass1"})
    assert r.status_code == 401


async def test_login_bruteforce_guard_also_counts_nonexistent_usernames(client, db_session):
    # Keyed by the attempted username string, not user_id — a guess against a
    # nonexistent account must still count, or the limiter itself becomes a
    # username-enumeration oracle (a real account would 401 differently once
    # blocked vs. an account that was never rate-limited).
    for _ in range(5):
        r = await client.post("/api/auth/login", json={"username": "NoSuchUserAtAll", "password": "whatever1"})
        assert r.status_code == 401
    assert "nosuchuseratall" in _failed_login_attempts


async def test_login_attempts_scoped_per_account(client, db_session):
    victim = await _make_user(db_session, "LoginBruteVictim2")
    bystander = await _make_user(db_session, "LoginBruteBystander")

    for _ in range(5):
        r = await client.post("/api/auth/login", json={"username": victim.username, "password": "wrongpass"})
        assert r.status_code == 401

    # A different account's correct password must still work.
    r = await client.post("/api/auth/login", json={"username": bystander.username, "password": "correctpass1"})
    assert r.status_code == 200


async def test_successful_login_resets_attempt_counter(client, db_session):
    user = await _make_user(db_session, "LoginBruteRecovers")

    for _ in range(3):
        r = await client.post("/api/auth/login", json={"username": user.username, "password": "wrongpass"})
        assert r.status_code == 401

    r = await client.post("/api/auth/login", json={"username": user.username, "password": "correctpass1"})
    assert r.status_code == 200
    assert user.username.lower() not in _failed_login_attempts
