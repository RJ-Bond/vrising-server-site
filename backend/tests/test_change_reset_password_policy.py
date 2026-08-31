"""ChangePasswordBody/ResetPasswordBody (backend/schemas.py) used to only enforce a
bare 6-character minimum, weaker than what registration (UserRegister.password_complexity)
required — the account-recovery/change path could land on a weaker credential than
signup ever allowed. Both now share UserRegister's same bar (_check_password_complexity:
length >= 8 plus at least one letter and one digit) via a common validator function."""

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User, PasswordReset
from datetime import datetime, timedelta, timezone

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username="pwpolicyuser", password="OldPass123"):
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash(password),
        role="user",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def test_change_password_rejects_short_new_password(client, db_session):
    user = await _make_user(db_session)
    r = await client.post(
        "/api/auth/change-password",
        json={"old_password": "OldPass123", "new_password": "ab1"},
        headers=_bearer(user),
    )
    assert r.status_code == 422


async def test_change_password_rejects_new_password_without_digit(client, db_session):
    user = await _make_user(db_session)
    r = await client.post(
        "/api/auth/change-password",
        json={"old_password": "OldPass123", "new_password": "onlyletters"},
        headers=_bearer(user),
    )
    assert r.status_code == 422


async def test_change_password_accepts_valid_new_password(client, db_session):
    user = await _make_user(db_session)
    r = await client.post(
        "/api/auth/change-password",
        json={"old_password": "OldPass123", "new_password": "newpass456"},
        headers=_bearer(user),
    )
    assert r.status_code == 200, r.text


async def test_reset_password_rejects_weak_new_password(client, db_session):
    user = await _make_user(db_session)
    reset = PasswordReset(
        user_id=user.id, token="test-reset-token-weak",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(reset)
    await db_session.commit()

    r = await client.post(
        "/api/auth/reset-password/test-reset-token-weak",
        json={"new_password": "123456"},
    )
    assert r.status_code == 422


async def test_reset_password_accepts_strong_new_password(client, db_session):
    user = await _make_user(db_session)
    reset = PasswordReset(
        user_id=user.id, token="test-reset-token-strong",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(reset)
    await db_session.commit()

    r = await client.post(
        "/api/auth/reset-password/test-reset-token-strong",
        json={"new_password": "freshpass789"},
    )
    assert r.status_code == 200, r.text
