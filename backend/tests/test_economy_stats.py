"""Regression tests for GET /api/admin/economy-stats — the points-economy dashboard
(issued vs spent per day from the PointsTransaction ledger, top redeemed shop items,
current total balance in circulation)."""
from datetime import datetime, timedelta

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import PointsTransaction, ShopRedemption, User

pytestmark = pytest.mark.asyncio


async def _make_admin(db_session, username="AdminUser"):
    admin = User(
        username=username, email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("adminpass1"), role="admin", points_balance=0,
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    return admin


async def _make_user(db_session, username, points_balance=0):
    user = User(
        username=username, email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("x"), role="user", points_balance=points_balance,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def test_requires_admin(client, db_session):
    user = await _make_user(db_session, "PlainUser")
    r = await client.get("/api/admin/economy-stats", headers=_bearer(user))
    assert r.status_code == 403


async def test_issued_and_spent_split_by_day(client, db_session):
    admin = await _make_admin(db_session)
    player = await _make_user(db_session, "Player1", points_balance=300)
    today = datetime.utcnow()
    db_session.add_all([
        PointsTransaction(user_id=player.id, delta=500, balance_after=500, reason="playtime", created_at=today),
        PointsTransaction(user_id=player.id, delta=-200, balance_after=300, reason="redeem", created_at=today),
    ])
    await db_session.commit()
    r = await client.get("/api/admin/economy-stats", headers=_bearer(admin), params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert len(body["by_day"]) == 1
    assert body["by_day"][0]["issued"] == 500
    assert body["by_day"][0]["spent"] == 200


async def test_balance_total_sums_all_users(client, db_session):
    admin = await _make_admin(db_session)
    await _make_user(db_session, "P1", points_balance=100)
    await _make_user(db_session, "P2", points_balance=250)
    r = await client.get("/api/admin/economy-stats", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json()["balance_total"] == 350


async def test_top_items_ordered_by_redemption_count(client, db_session):
    admin = await _make_admin(db_session)
    player = await _make_user(db_session, "Player2")
    now = datetime.utcnow()
    db_session.add_all([
        ShopRedemption(user_id=player.id, item_name_snapshot="Sword", cost_snapshot=100, status="fulfilled", created_at=now),
        ShopRedemption(user_id=player.id, item_name_snapshot="Sword", cost_snapshot=100, status="pending", created_at=now),
        ShopRedemption(user_id=player.id, item_name_snapshot="Shield", cost_snapshot=50, status="fulfilled", created_at=now),
    ])
    await db_session.commit()
    r = await client.get("/api/admin/economy-stats", headers=_bearer(admin))
    assert r.status_code == 200
    top_items = r.json()["top_items"]
    assert top_items[0] == {"name": "Sword", "redemptions": 2, "points_spent": 200}
    assert top_items[1] == {"name": "Shield", "redemptions": 1, "points_spent": 50}


async def test_cancelled_redemptions_excluded_from_top_items(client, db_session):
    admin = await _make_admin(db_session)
    player = await _make_user(db_session, "Player3")
    db_session.add(ShopRedemption(
        user_id=player.id, item_name_snapshot="Refunded Item", cost_snapshot=100,
        status="cancelled", created_at=datetime.utcnow(),
    ))
    await db_session.commit()
    r = await client.get("/api/admin/economy-stats", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json()["top_items"] == []


async def test_transactions_outside_window_excluded(client, db_session):
    admin = await _make_admin(db_session)
    player = await _make_user(db_session, "Player4")
    old = datetime.utcnow() - timedelta(days=60)
    db_session.add(PointsTransaction(user_id=player.id, delta=999, balance_after=999, reason="playtime", created_at=old))
    await db_session.commit()
    r = await client.get("/api/admin/economy-stats", headers=_bearer(admin), params={"days": 7})
    assert r.status_code == 200
    assert r.json()["by_day"] == []
