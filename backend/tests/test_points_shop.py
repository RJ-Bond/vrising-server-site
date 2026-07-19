"""Regression tests for the v1 points economy: playtime/streak earning hooks
(POST /api/plugin/sessions, POST /api/plugin/connect-streak), the shop catalog/redeem
flow (GET /api/shop/items, POST /api/shop/redeem, GET /api/shop/redemptions/me), the
admin redemption queue (fulfill/cancel), manual points grants, and the atomic
conditional-UPDATE redeem path's concurrency safety. See models.py's
PointsTransaction/ShopItem/ShopRedemption and the "Points economy" sections of main.py."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import PlayerDailyActivity, PlayerRecord, PointsTransaction, Setting, ShopItem, ShopRedemption, User

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-points"


async def _set_plugin_key(db_session, value=PLUGIN_KEY, timezone_val="UTC"):
    db_session.add(Setting(key="plugin_api_key", value=value))
    db_session.add(Setting(key="timezone", value=timezone_val))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def _make_user(db_session, username, steam_id=None, role="user", points_balance=0):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
        steam_id=steam_id,
        points_balance=points_balance,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


# ─── Playtime earning hook (POST /api/plugin/sessions) ─────────────────────────

async def test_playtime_session_awards_points_to_linked_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "PlayLinked", steam_id="76500000000000101")

    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": user.steam_id, "character_name": "PlayLinked", "session_seconds": 600},
        headers=_hdr(),
    )
    assert r.status_code == 200

    await db_session.refresh(user)
    # default points_per_minute_playtime=1, 600s = 10 minutes -> 10 points
    assert user.points_balance == 10

    tx_rows = (await db_session.execute(select(PointsTransaction).where(PointsTransaction.user_id == user.id))).scalars().all()
    assert len(tx_rows) == 1
    assert tx_rows[0].delta == 10
    assert tx_rows[0].reason == "playtime"
    assert tx_rows[0].balance_after == 10


async def test_playtime_session_is_noop_for_unlinked_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000999", "character_name": "GhostPlayer", "session_seconds": 600},
        headers=_hdr(),
    )
    assert r.status_code == 200
    # PlayerRecord update must still proceed unchanged even with no linked user.
    rec = (await db_session.execute(
        select(PlayerRecord).where(PlayerRecord.steam_id == "76500000000000999")
    )).scalar_one_or_none()
    assert rec is not None
    assert rec.total_seconds == 600
    tx_rows = (await db_session.execute(select(PointsTransaction))).scalars().all()
    assert tx_rows == []


async def test_playtime_session_under_one_minute_awards_zero(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "ShortSession", steam_id="76500000000000102")
    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": user.steam_id, "character_name": "ShortSession", "session_seconds": 30},
        headers=_hdr(),
    )
    assert r.status_code == 200
    await db_session.refresh(user)
    assert user.points_balance == 0


# ─── Streak-bonus earning hook (POST /api/plugin/connect-streak) ───────────────

async def test_streak_bonus_fires_once_streak_reaches_min_days(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76500000000000201"
    user = await _make_user(db_session, "StreakPlayer", steam_id=steam_id)

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    db_session.add(PlayerDailyActivity(server_num=1, steam_id=steam_id, activity_date=yesterday))
    await db_session.commit()

    # First connect today -> streak_days=2, meets default points_streak_min_days=2 -> bonus.
    r = await client.post("/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"streak_days": 2}

    await db_session.refresh(user)
    assert user.points_balance == 10  # default points_streak_bonus=10

    # A second connect-streak call the same day must NOT double-award (idempotent day).
    r2 = await client.post("/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr())
    assert r2.status_code == 200
    assert r2.json() == {"streak_days": 2}
    await db_session.refresh(user)
    assert user.points_balance == 10


async def test_streak_bonus_does_not_fire_below_min_days(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76500000000000202"
    user = await _make_user(db_session, "FreshStreak", steam_id=steam_id)

    # First ever connect -> streak_days=1, below default min of 2 -> no bonus.
    r = await client.post("/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"streak_days": 1}

    await db_session.refresh(user)
    assert user.points_balance == 0


# ─── GET /api/auth/me returns points_balance ────────────────────────────────

async def test_auth_me_returns_points_balance(client, db_session):
    user = await _make_user(db_session, "MeUser", points_balance=42)
    r = await client.get("/api/auth/me", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["points_balance"] == 42


# ─── Shop catalog + redeem ──────────────────────────────────────────────────

async def _make_item(db_session, name="Blood Rose Seeds", cost=50, stock=None, is_active=True):
    item = ShopItem(name=name, cost=cost, stock=stock, is_active=is_active)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


async def test_list_shop_items_requires_login(client, db_session):
    r = await client.get("/api/shop/items")
    assert r.status_code == 401


async def test_list_shop_items_excludes_inactive(client, db_session):
    user = await _make_user(db_session, "Shopper1", points_balance=100)
    await _make_item(db_session, name="Active Item", is_active=True)
    await _make_item(db_session, name="Hidden Item", is_active=False)

    r = await client.get("/api/shop/items", headers=_bearer(user))
    assert r.status_code == 200
    names = [i["name"] for i in r.json()]
    assert "Active Item" in names
    assert "Hidden Item" not in names


async def test_redeem_happy_path_deducts_balance_and_creates_redemption(client, db_session):
    user = await _make_user(db_session, "Redeemer1", points_balance=100)
    item = await _make_item(db_session, name="Waypoint Shard", cost=30)

    r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id, "note": "pls"}, headers=_bearer(user))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["cost_snapshot"] == 30
    assert body["item_name_snapshot"] == "Waypoint Shard"

    await db_session.refresh(user)
    assert user.points_balance == 70

    tx_rows = (await db_session.execute(select(PointsTransaction).where(PointsTransaction.user_id == user.id))).scalars().all()
    assert len(tx_rows) == 1
    assert tx_rows[0].delta == -30
    assert tx_rows[0].balance_after == 70
    assert tx_rows[0].reason == "redeem"


async def test_redeem_insufficient_balance_rejected(client, db_session):
    user = await _make_user(db_session, "PoorPlayer", points_balance=10)
    item = await _make_item(db_session, name="Expensive Item", cost=9999)

    r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r.status_code == 400

    await db_session.refresh(user)
    assert user.points_balance == 10
    redemptions = (await db_session.execute(select(ShopRedemption))).scalars().all()
    assert redemptions == []


async def test_redeem_out_of_stock_rejected(client, db_session):
    user = await _make_user(db_session, "StockChecker", points_balance=1000)
    item = await _make_item(db_session, name="Limited Item", cost=10, stock=0)

    r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r.status_code == 409


async def test_redeem_out_of_stock_does_not_leave_balance_deducted(client, db_session):
    """The stock check happens after the balance-deducting UPDATE; if stock is
    unavailable the whole request must roll back, not just skip the stock decrement."""
    user = await _make_user(db_session, "StockRollback", points_balance=1000)
    item = await _make_item(db_session, name="Limited Item 2", cost=10, stock=0)

    r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r.status_code == 409

    fresh = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert fresh.points_balance == 1000


async def test_redeem_inactive_item_404s(client, db_session):
    user = await _make_user(db_session, "InactiveBuyer", points_balance=100)
    item = await _make_item(db_session, name="Delisted Item", cost=10, is_active=False)

    r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r.status_code == 404


async def test_my_shop_redemptions_returns_own_history(client, db_session):
    user = await _make_user(db_session, "HistoryUser", points_balance=100)
    item = await _make_item(db_session, name="History Item", cost=20)
    r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r.status_code == 201

    r2 = await client.get("/api/shop/redemptions/me", headers=_bearer(user))
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] == 1
    assert body["items"][0]["item_name_snapshot"] == "History Item"


async def test_my_points_transactions_includes_earn_and_spend(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76500000000000301"
    user = await _make_user(db_session, "LedgerUser", steam_id=steam_id, points_balance=0)

    r1 = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": steam_id, "character_name": "LedgerUser", "session_seconds": 600},
        headers=_hdr(),
    )
    assert r1.status_code == 200

    item = await _make_item(db_session, name="Ledger Item", cost=5)
    r2 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r2.status_code == 201

    r3 = await client.get("/api/points/transactions/me", headers=_bearer(user))
    assert r3.status_code == 200
    body = r3.json()
    assert body["total"] == 2
    reasons = {t["reason"] for t in body["items"]}
    assert reasons == {"playtime", "redeem"}


# ─── Concurrent double-redeem — regression test for the atomic conditional UPDATE ──

async def test_concurrent_double_redeem_only_one_succeeds(client, db_session):
    """The direct regression test for the atomic-UPDATE approach in POST /api/shop/redeem:
    a naive "read balance, check in Python, then UPDATE" would let two concurrent requests
    both pass the balance check before either commits, double-spending the same points.
    Give the user exactly enough for ONE purchase and fire two redeem requests
    concurrently — exactly one must succeed (201) and the other must be rejected (400),
    and the final balance must reflect only a single deduction."""
    user = await _make_user(db_session, "RaceUser", points_balance=50)
    item = await _make_item(db_session, name="Race Item", cost=50, stock=None)
    headers = _bearer(user)

    async def _attempt():
        return await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=headers)

    r1, r2 = await asyncio.gather(_attempt(), _attempt())
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [201, 400]

    # db_session has expire_on_commit=False and `user` is already in its identity map
    # (loaded by _make_user above) — a plain re-select would return the cached, stale
    # attribute values rather than what the concurrent requests (each on their own
    # session) actually committed. Force a real reload.
    await db_session.refresh(user)
    assert user.points_balance == 0

    redemptions = (await db_session.execute(select(ShopRedemption).where(ShopRedemption.user_id == user.id))).scalars().all()
    assert len(redemptions) == 1


async def test_concurrent_double_redeem_respects_limited_stock(client, db_session):
    """Same race, but for stock=1 instead of balance — only one of two concurrent
    requests for the same last-unit item may succeed."""
    user = await _make_user(db_session, "StockRaceUser", points_balance=1000)
    item = await _make_item(db_session, name="Last Unit", cost=10, stock=1)
    headers = _bearer(user)

    async def _attempt():
        return await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=headers)

    r1, r2 = await asyncio.gather(_attempt(), _attempt())
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [201, 409]

    # Same identity-map staleness concern as the balance-race test above — `item` is
    # already loaded in db_session's identity map, so force a real reload.
    await db_session.refresh(item)
    assert item.stock == 0


# ─── Admin redemption queue: fulfill / cancel-refund ────────────────────────

async def test_fulfill_redemption_marks_resolved(client, db_session):
    admin = await _make_user(db_session, "AdminFulfill", role="admin")
    user = await _make_user(db_session, "FulfillTarget", points_balance=100)
    item = await _make_item(db_session, name="Fulfill Item", cost=10)
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redemption_id = redeem_r.json()["id"]

    r = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/fulfill", json={"admin_note": "delivered"}, headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "fulfilled"
    assert body["resolved_by"] == "AdminFulfill"
    assert body["admin_note"] == "delivered"


async def test_cancel_redemption_refunds_points(client, db_session):
    admin = await _make_user(db_session, "AdminCancel", role="admin")
    user = await _make_user(db_session, "CancelTarget", points_balance=100)
    item = await _make_item(db_session, name="Cancel Item", cost=40)
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redemption_id = redeem_r.json()["id"]

    await db_session.refresh(user)
    assert user.points_balance == 60

    r = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/cancel", json={}, headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # `user` is already in db_session's identity map (refreshed above to the post-redeem
    # balance) — expire_on_commit=False means a plain re-select would return that cached
    # value rather than what the admin's cancel request (its own session) just committed.
    await db_session.refresh(user)
    assert user.points_balance == 100  # refunded in full


async def test_double_cancel_409s_instead_of_double_refunding(client, db_session):
    admin = await _make_user(db_session, "AdminDoubleCancel", role="admin")
    user = await _make_user(db_session, "DoubleCancelTarget", points_balance=100)
    item = await _make_item(db_session, name="Double Cancel Item", cost=25)
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redemption_id = redeem_r.json()["id"]

    r1 = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/cancel", json={}, headers=_bearer(admin))
    assert r1.status_code == 200
    r2 = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/cancel", json={}, headers=_bearer(admin))
    assert r2.status_code == 409

    # Force a real reload (see the identity-map note on test_cancel_redemption_refunds_points
    # above) — this particular assertion happened to pass even without it since the net
    # change here is zero, but a plain select is the wrong tool regardless.
    await db_session.refresh(user)
    assert user.points_balance == 100  # only refunded once, not twice


async def test_fulfill_of_already_resolved_redemption_409s(client, db_session):
    admin = await _make_user(db_session, "AdminDoubleFulfill", role="admin")
    user = await _make_user(db_session, "DoubleFulfillTarget", points_balance=100)
    item = await _make_item(db_session, name="Double Fulfill Item", cost=15)
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redemption_id = redeem_r.json()["id"]

    r1 = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/fulfill", json={}, headers=_bearer(admin))
    assert r1.status_code == 200
    r2 = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/fulfill", json={}, headers=_bearer(admin))
    assert r2.status_code == 409


async def test_shop_redemption_endpoints_require_admin(client, db_session):
    user = await _make_user(db_session, "NotAnAdmin", role="user")
    r = await client.get("/api/admin/shop/redemptions", headers=_bearer(user))
    assert r.status_code == 403


# ─── Admin manual points grant ──────────────────────────────────────────────

async def test_admin_grant_points_adds_to_balance(client, db_session):
    admin = await _make_user(db_session, "AdminGranter", role="admin")
    user = await _make_user(db_session, "GrantTarget", points_balance=0)

    r = await client.post(
        "/api/admin/points/grant",
        json={"user_id": user.id, "delta": 500, "reason": "donation", "note": "Thanks!"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["delta"] == 500
    assert body["balance_after"] == 500
    assert body["username"] == "GrantTarget"

    await db_session.refresh(user)
    assert user.points_balance == 500


async def test_admin_grant_negative_delta_for_correction(client, db_session):
    admin = await _make_user(db_session, "AdminCorrector", role="admin")
    user = await _make_user(db_session, "CorrectionTarget", points_balance=100)

    r = await client.post(
        "/api/admin/points/grant",
        json={"user_id": user.id, "delta": -30, "reason": "admin_adjust"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    await db_session.refresh(user)
    assert user.points_balance == 70


async def test_admin_grant_zero_delta_rejected(client, db_session):
    admin = await _make_user(db_session, "AdminZero", role="admin")
    user = await _make_user(db_session, "ZeroTarget", points_balance=100)

    r = await client.post(
        "/api/admin/points/grant",
        json={"user_id": user.id, "delta": 0},
        headers=_bearer(admin),
    )
    assert r.status_code == 422  # pydantic validation error


async def test_admin_grant_requires_admin_role(client, db_session):
    user = await _make_user(db_session, "NotAdminGranter", role="user")
    target = await _make_user(db_session, "SomeTarget", points_balance=0)
    r = await client.post(
        "/api/admin/points/grant",
        json={"user_id": target.id, "delta": 10},
        headers=_bearer(user),
    )
    assert r.status_code == 403


# ─── Admin shop catalog CRUD ─────────────────────────────────────────────────

async def test_admin_create_update_delete_shop_item(client, db_session):
    admin = await _make_user(db_session, "AdminCatalog", role="admin")

    r = await client.post(
        "/api/admin/shop/items",
        json={"name": "New Item", "cost": 25, "description": "desc", "stock": 10},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    item_id = r.json()["id"]

    r2 = await client.put(
        f"/api/admin/shop/items/{item_id}",
        json={"cost": 40, "is_active": False},
        headers=_bearer(admin),
    )
    assert r2.status_code == 200
    assert r2.json()["cost"] == 40
    assert r2.json()["is_active"] is False

    r3 = await client.delete(f"/api/admin/shop/items/{item_id}", headers=_bearer(admin))
    assert r3.status_code == 204

    r4 = await client.get("/api/admin/shop/items", headers=_bearer(admin))
    assert item_id not in [i["id"] for i in r4.json()]
