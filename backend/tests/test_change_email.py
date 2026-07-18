"""Regression tests for POST /api/auth/change-email — previously missing entirely
(frontend called it, backend had no route, every save failed with 404)."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, email=None, password="correct horse battery staple"):
    user = User(
        username=username,
        email=email or f"{username}@example.com",
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


async def test_change_email_requires_auth(client, db_session):
    r = await client.post("/api/auth/change-email", json={"new_email": "new@example.com", "password": "whatever1"})
    assert r.status_code == 401


async def test_change_email_rejects_wrong_password(client, db_session):
    user = await _make_user(db_session, "Alice", password="realpassword1")
    r = await client.post(
        "/api/auth/change-email",
        json={"new_email": "alice-new@example.com", "password": "wrongpassword"},
        headers=_bearer(user),
    )
    assert r.status_code == 400


async def test_change_email_updates_with_correct_password(client, db_session):
    user = await _make_user(db_session, "Bob", email="bob@example.com", password="realpassword1")
    r = await client.post(
        "/api/auth/change-email",
        json={"new_email": "bob-new@example.com", "password": "realpassword1"},
        headers=_bearer(user),
    )
    assert r.status_code == 200
    assert r.json()["email"] == "bob-new@example.com"

    await db_session.refresh(user)
    assert user.email == "bob-new@example.com"


async def test_change_email_rejects_duplicate(client, db_session):
    await _make_user(db_session, "Existing", email="taken@example.com", password="password1")
    user = await _make_user(db_session, "Carol", email="carol@example.com", password="realpassword1")
    r = await client.post(
        "/api/auth/change-email",
        json={"new_email": "taken@example.com", "password": "realpassword1"},
        headers=_bearer(user),
    )
    assert r.status_code == 400


async def test_change_email_this_users_own_current_email_is_not_a_conflict(client, db_session):
    # Re-submitting the same email you already have shouldn't trip the "already used" check.
    user = await _make_user(db_session, "Dave", email="dave@example.com", password="realpassword1")
    r = await client.post(
        "/api/auth/change-email",
        json={"new_email": "dave@example.com", "password": "realpassword1"},
        headers=_bearer(user),
    )
    assert r.status_code == 200
