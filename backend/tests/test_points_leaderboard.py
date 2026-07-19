"""Regression tests for the points-economy leaderboard (GET /api/leaderboard/points,
see main.py's get_points_leaderboard). Ranks User rows by points_balance descending,
separate from the playtime leaderboard (GET /api/leaderboard, PlayerRecord-based) since
points_balance is a single global balance per site account, not per-server."""
import pytest

from backend.auth import get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, points_balance=0, is_active=True):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        points_balance=points_balance,
        is_active=is_active,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_points_leaderboard_orders_by_balance_descending(client, db_session):
    await _make_user(db_session, "Alice", points_balance=500)
    await _make_user(db_session, "Bob", points_balance=1500)
    await _make_user(db_session, "Carol", points_balance=10)

    r = await client.get("/api/leaderboard/points")
    assert r.status_code == 200
    items = r.json()
    assert [it["username"] for it in items] == ["Bob", "Alice", "Carol"]
    assert [it["points_balance"] for it in items] == [1500, 500, 10]


async def test_points_leaderboard_excludes_zero_and_negative_balances(client, db_session):
    await _make_user(db_session, "ZeroGuy", points_balance=0)
    await _make_user(db_session, "InDebt", points_balance=-50)
    await _make_user(db_session, "HasPoints", points_balance=5)

    r = await client.get("/api/leaderboard/points")
    assert r.status_code == 200
    items = r.json()
    assert [it["username"] for it in items] == ["HasPoints"]


async def test_points_leaderboard_excludes_deactivated_accounts(client, db_session):
    await _make_user(db_session, "ActiveUser", points_balance=100, is_active=True)
    await _make_user(db_session, "BannedUser", points_balance=9999, is_active=False)

    r = await client.get("/api/leaderboard/points")
    assert r.status_code == 200
    items = r.json()
    assert [it["username"] for it in items] == ["ActiveUser"]


async def test_points_leaderboard_pagination(client, db_session):
    for i in range(5):
        await _make_user(db_session, f"P{i}", points_balance=100 - i)

    r = await client.get("/api/leaderboard/points", params={"page": 2, "per_page": 2})
    assert r.status_code == 200
    items = r.json()
    assert [it["username"] for it in items] == ["P2", "P3"]


async def test_points_leaderboard_is_public_no_auth_required(client, db_session):
    await _make_user(db_session, "Solo", points_balance=42)
    r = await client.get("/api/leaderboard/points")
    assert r.status_code == 200


async def test_points_leaderboard_empty_when_no_positive_balances(client, db_session):
    r = await client.get("/api/leaderboard/points")
    assert r.status_code == 200
    assert r.json() == []
