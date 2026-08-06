"""Regression tests for GET /api/users/recent — the most-recently-registered active
users, backing the homepage "Новые игроки" avatar-strip widget (frontend/index.js's
loadNewPlayers()). Public, unauthenticated, same visibility as GET /api/users search.

Registered before GET /api/users/{username} in backend/routers/users.py (same ordering
requirement GET /api/clans/leaderboard documents for GET /api/clans/{clan_id}) — a test
here also guards against that route ordering regressing, since "recent" would otherwise
be swallowed as a {username} path param and 404 against a real lookup instead."""
import pytest

from backend.auth import get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, **kwargs):
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("x"),
        **kwargs,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_empty_when_no_users(client, db_session):
    r = await client.get("/api/users/recent")
    assert r.status_code == 200
    assert r.json() == []


async def test_route_registered_before_username_param_route(client, db_session):
    # If GET /api/users/{username} were matched first, this would 404 (no user named
    # "recent") instead of returning the recent-users list.
    r = await client.get("/api/users/recent")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_most_recently_registered_first(client, db_session):
    await _make_user(db_session, "Oldest")
    await _make_user(db_session, "Middle")
    await _make_user(db_session, "Newest")

    r = await client.get("/api/users/recent")
    assert r.status_code == 200
    usernames = [u["username"] for u in r.json()]
    assert usernames == ["Newest", "Middle", "Oldest"]


async def test_inactive_users_excluded(client, db_session):
    await _make_user(db_session, "ActiveOne")
    await _make_user(db_session, "BannedOne", is_active=False)

    r = await client.get("/api/users/recent")
    usernames = [u["username"] for u in r.json()]
    assert usernames == ["ActiveOne"]


async def test_response_shape(client, db_session):
    await _make_user(db_session, "ShapeUser", avatar_url="https://example.com/a.png")

    r = await client.get("/api/users/recent")
    body = r.json()
    assert len(body) == 1
    assert set(body[0].keys()) == {"username", "avatar_url", "created_at"}
    assert body[0]["username"] == "ShapeUser"
    assert body[0]["avatar_url"] == "https://example.com/a.png"
    assert body[0]["created_at"]  # non-empty formatted string


async def test_limit_param_respected(client, db_session):
    for i in range(5):
        await _make_user(db_session, f"LimitUser{i}")

    r = await client.get("/api/users/recent", params={"limit": 2})
    assert len(r.json()) == 2
