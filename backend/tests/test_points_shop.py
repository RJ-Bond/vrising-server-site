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
from backend.models import PlayerDailyActivity, PlayerRecord, PointsTransaction, Setting, ShopItem, ShopRedemption, ShopWishlistItem, User

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

async def _make_item(db_session, name="Blood Rose Seeds", cost=50, stock=None, is_active=True, category=None, weekly_limit_per_user=None):
    item = ShopItem(name=name, cost=cost, stock=stock, is_active=is_active, category=category, weekly_limit_per_user=weekly_limit_per_user)
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


async def test_concurrent_double_redeem_respects_weekly_limit(client, db_session):
    """Same race, for weekly_limit_per_user instead of balance/stock: the pre-check
    (read recent_count, then decide) that guards the weekly limit is a plain read-then-act
    with no atomic UPDATE backing it, unlike balance/stock immediately below it in
    POST /api/shop/redeem — so two concurrent requests, both seeing the same
    not-yet-at-limit recent_count, could otherwise both be admitted and push the user
    over weekly_limit_per_user. Give the user plenty of balance/stock (so those checks
    can't be what blocks the second request) and a weekly_limit_per_user=1 item with zero
    prior redemptions — only one of two concurrent requests may succeed."""
    user = await _make_user(db_session, "WeeklyLimitRaceUser", points_balance=1000)
    item = await _make_item(db_session, name="Weekly Race Item", cost=10, weekly_limit_per_user=1)
    headers = _bearer(user)

    async def _attempt():
        return await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=headers)

    r1, r2 = await asyncio.gather(_attempt(), _attempt())
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [201, 409]

    redemptions = (await db_session.execute(
        select(ShopRedemption).where(ShopRedemption.user_id == user.id, ShopRedemption.status != "cancelled")
    )).scalars().all()
    assert len(redemptions) == 1

    # The rejected attempt's balance UPDATE must have been rolled back along with it —
    # only the one successful redemption's cost should be deducted.
    await db_session.refresh(user)
    assert user.points_balance == 990


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


# ─── Admin queue: weekly-limit context (ShopRedemptionOut.weekly_limit_per_user/
# weekly_used) ─────────────────────────────────────────────────────────────────
# Lets an admin see "this player is at/near this item's weekly cap" directly in the
# queue table, without waiting for the player's next attempt to 409 (see POST
# /api/shop/redeem's docstring on the underlying check this mirrors).

async def test_queue_surfaces_weekly_limit_and_usage(client, db_session):
    admin = await _make_user(db_session, "AdminQueueLimit", role="admin")
    user = await _make_user(db_session, "QueueLimitTarget", points_balance=1000)
    item = await _make_item(db_session, name="Limited Item", cost=10, weekly_limit_per_user=3)
    # Two redemptions this week — the second one leaves the row we'll inspect.
    await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert redeem_r.status_code == 201

    r = await client.get("/api/admin/shop/redemptions", params={"status": ""}, headers=_bearer(admin))
    assert r.status_code == 200
    rows = r.json()["items"]
    assert len(rows) == 2
    for row in rows:
        assert row["weekly_limit_per_user"] == 3
        assert row["weekly_used"] == 2


async def test_queue_cancelled_redemption_excluded_from_weekly_used(client, db_session):
    admin = await _make_user(db_session, "AdminQueueLimit2", role="admin")
    user = await _make_user(db_session, "QueueLimitTarget2", points_balance=1000)
    item = await _make_item(db_session, name="Limited Item 2", cost=10, weekly_limit_per_user=5)
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redemption_id = redeem_r.json()["id"]
    cancel_r = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/cancel", json={}, headers=_bearer(admin))
    assert cancel_r.status_code == 200

    r = await client.get("/api/admin/shop/redemptions", params={"status": "cancelled"}, headers=_bearer(admin))
    assert r.status_code == 200
    row = r.json()["items"][0]
    assert row["weekly_limit_per_user"] == 5
    assert row["weekly_used"] == 0  # the cancelled redemption itself doesn't count


async def test_queue_omits_weekly_fields_when_item_has_no_limit(client, db_session):
    admin = await _make_user(db_session, "AdminQueueLimit3", role="admin")
    user = await _make_user(db_session, "QueueLimitTarget3", points_balance=1000)
    item = await _make_item(db_session, name="Unlimited Item", cost=10, weekly_limit_per_user=None)
    await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))

    r = await client.get("/api/admin/shop/redemptions", params={"status": ""}, headers=_bearer(admin))
    assert r.status_code == 200
    row = r.json()["items"][0]
    assert row["weekly_limit_per_user"] is None
    assert row["weekly_used"] is None


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


# ─── Admin bulk points grant ─────────────────────────────────────────────────

async def test_admin_grant_bulk_all_valid_identifiers_succeed(client, db_session):
    admin = await _make_user(db_session, "AdminBulkGranter", role="admin")
    u1 = await _make_user(db_session, "BulkTarget1", steam_id="76500000000000201", points_balance=0)
    u2 = await _make_user(db_session, "BulkTarget2", points_balance=10)

    r = await client.post(
        "/api/admin/points/grant-bulk",
        json={"identifiers": ["BulkTarget1", u2.username], "delta": 100, "reason": "donation", "note": "batch"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    results_by_identifier = {row["identifier"]: row for row in body["results"]}
    assert results_by_identifier["BulkTarget1"]["success"] is True
    assert results_by_identifier["BulkTarget1"]["balance_after"] == 100
    assert results_by_identifier[u2.username]["success"] is True
    assert results_by_identifier[u2.username]["balance_after"] == 110

    await db_session.refresh(u1)
    await db_session.refresh(u2)
    assert u1.points_balance == 100
    assert u2.points_balance == 110


async def test_admin_grant_bulk_by_steam_id(client, db_session):
    admin = await _make_user(db_session, "AdminBulkGranterSteam", role="admin")
    user = await _make_user(db_session, "SteamBulkTarget", steam_id="76500000000000202", points_balance=0)

    r = await client.post(
        "/api/admin/points/grant-bulk",
        json={"identifiers": [user.steam_id], "delta": 50},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 1
    assert body["results"][0]["username"] == "SteamBulkTarget"

    await db_session.refresh(user)
    assert user.points_balance == 50


async def test_admin_grant_bulk_mixed_valid_invalid_reports_per_entry(client, db_session):
    admin = await _make_user(db_session, "AdminBulkMixed", role="admin")
    valid_user = await _make_user(db_session, "ValidBulkTarget", points_balance=0)

    r = await client.post(
        "/api/admin/points/grant-bulk",
        json={"identifiers": ["ValidBulkTarget", "NoSuchUserAtAll"], "delta": 25},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    results_by_identifier = {row["identifier"]: row for row in body["results"]}
    assert results_by_identifier["ValidBulkTarget"]["success"] is True
    assert results_by_identifier["NoSuchUserAtAll"]["success"] is False
    assert results_by_identifier["NoSuchUserAtAll"]["error"]

    # The valid grant must still have gone through despite the other entry failing.
    await db_session.refresh(valid_user)
    assert valid_user.points_balance == 25


async def test_admin_grant_bulk_requires_admin_role(client, db_session):
    user = await _make_user(db_session, "NotAdminBulkGranter", role="user")
    target = await _make_user(db_session, "BulkForbiddenTarget", points_balance=0)
    r = await client.post(
        "/api/admin/points/grant-bulk",
        json={"identifiers": [target.username], "delta": 10},
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


# ─── Admin redemptions CSV export ───────────────────────────────────────────

async def test_export_redemptions_requires_login(client, db_session):
    r = await client.get("/api/admin/export/redemptions")
    assert r.status_code == 401


async def test_export_redemptions_rejects_under_privileged_user(client, db_session):
    user = await _make_user(db_session, "NotModExporter", role="user")
    r = await client.get("/api/admin/export/redemptions", headers=_bearer(user))
    assert r.status_code == 403


async def test_export_redemptions_returns_csv_for_moderator(client, db_session):
    # Fulfilling a redemption requires admin (get_admin_user), not moderator — use a
    # separate admin to set up the fixture data, then verify the export endpoint itself
    # (gated on the lower get_moderator_user tier, matching export/bans) is reachable
    # by a plain moderator.
    admin = await _make_user(db_session, "AdminFulfillerForExport", role="admin")
    moderator = await _make_user(db_session, "ModExporter", role="moderator")
    user = await _make_user(db_session, "ExportTarget", points_balance=100)
    item = await _make_item(db_session, name="Export Item", cost=15)
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redemption_id = redeem_r.json()["id"]
    fulfill_r = await client.post(
        f"/api/admin/shop/redemptions/{redemption_id}/fulfill",
        json={"admin_note": "shipped"},
        headers=_bearer(admin),
    )
    assert fulfill_r.status_code == 200

    r = await client.get("/api/admin/export/redemptions", headers=_bearer(moderator))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=redemptions.csv" in r.headers["content-disposition"]

    body = r.text
    lines = body.strip().splitlines()
    assert lines[0] == "user,item,cost,status,created_at,resolved_at,resolved_by,admin_note"
    data_lines = lines[1:]
    assert len(data_lines) == 1
    row = data_lines[0]
    assert row.startswith("ExportTarget,Export Item,15,fulfilled,")
    assert "AdminFulfillerForExport" in row
    assert "shipped" in row


# ─── Shop wishlist ────────────────────────────────────────────────────────

async def test_wishlist_add_then_remove_toggles_state(client, db_session):
    user = await _make_user(db_session, "WishToggle", points_balance=100)
    item = await _make_item(db_session, name="Wish Item")

    r = await client.post(f"/api/shop/wishlist/{item.id}", headers=_bearer(user))
    assert r.status_code == 201
    assert r.json() == {"shop_item_id": item.id, "wishlisted": True}

    rows = (await db_session.execute(select(ShopWishlistItem).where(ShopWishlistItem.user_id == user.id))).scalars().all()
    assert len(rows) == 1

    r2 = await client.delete(f"/api/shop/wishlist/{item.id}", headers=_bearer(user))
    assert r2.status_code == 200
    assert r2.json() == {"shop_item_id": item.id, "wishlisted": False}

    rows2 = (await db_session.execute(select(ShopWishlistItem).where(ShopWishlistItem.user_id == user.id))).scalars().all()
    assert rows2 == []


async def test_wishlist_add_is_idempotent(client, db_session):
    user = await _make_user(db_session, "WishDupe", points_balance=100)
    item = await _make_item(db_session, name="Dupe Item")

    r1 = await client.post(f"/api/shop/wishlist/{item.id}", headers=_bearer(user))
    assert r1.status_code == 201
    r2 = await client.post(f"/api/shop/wishlist/{item.id}", headers=_bearer(user))
    assert r2.status_code == 201
    assert r2.json()["wishlisted"] is True

    rows = (await db_session.execute(select(ShopWishlistItem).where(ShopWishlistItem.user_id == user.id))).scalars().all()
    assert len(rows) == 1  # the unique constraint didn't 500 on the second call — it was a no-op


async def test_wishlist_remove_of_never_added_item_is_idempotent(client, db_session):
    user = await _make_user(db_session, "WishNeverAdded", points_balance=100)
    item = await _make_item(db_session, name="Never Added Item")

    r = await client.delete(f"/api/shop/wishlist/{item.id}", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["wishlisted"] is False


async def test_wishlist_me_returns_only_current_user_items(client, db_session):
    user1 = await _make_user(db_session, "WishOwner", points_balance=100)
    user2 = await _make_user(db_session, "WishOther", points_balance=100)
    item1 = await _make_item(db_session, name="Owner Item")
    item2 = await _make_item(db_session, name="Other Item")

    r1 = await client.post(f"/api/shop/wishlist/{item1.id}", headers=_bearer(user1))
    assert r1.status_code == 201
    r2 = await client.post(f"/api/shop/wishlist/{item2.id}", headers=_bearer(user2))
    assert r2.status_code == 201

    r = await client.get("/api/shop/wishlist/me", headers=_bearer(user1))
    assert r.status_code == 200
    body = r.json()
    names = [i["name"] for i in body]
    assert names == ["Owner Item"]
    assert body[0]["wishlisted"] is True


async def test_wishlist_add_to_nonexistent_item_404s(client, db_session):
    user = await _make_user(db_session, "WishGhost", points_balance=100)
    r = await client.post("/api/shop/wishlist/999999", headers=_bearer(user))
    assert r.status_code == 404


async def test_wishlist_survives_and_does_not_500_when_item_later_deleted(client, db_session):
    """This slice picked CASCADE for ShopWishlistItem.shop_item_id (unlike
    ShopRedemption's SET NULL, since there's no purchase snapshot worth preserving for a
    plain wishlist row) — deleting the catalog item should quietly drop the wishlist row
    rather than error, and GET /api/shop/wishlist/me must not 500 afterward."""
    admin = await _make_user(db_session, "WishDeleteAdmin", role="admin")
    user = await _make_user(db_session, "WishDeleteUser", points_balance=100)
    item = await _make_item(db_session, name="Doomed Item")

    r = await client.post(f"/api/shop/wishlist/{item.id}", headers=_bearer(user))
    assert r.status_code == 201

    del_r = await client.delete(f"/api/admin/shop/items/{item.id}", headers=_bearer(admin))
    assert del_r.status_code == 204

    r2 = await client.get("/api/shop/wishlist/me", headers=_bearer(user))
    assert r2.status_code == 200
    assert r2.json() == []

    rows = (await db_session.execute(select(ShopWishlistItem).where(ShopWishlistItem.user_id == user.id))).scalars().all()
    assert rows == []


async def test_wishlist_requires_login(client, db_session):
    r = await client.get("/api/shop/wishlist/me")
    assert r.status_code == 401


# ─── Shop per-item weekly purchase limit ────────────────────────────────────

async def test_weekly_limit_blocks_redemption_once_reached(client, db_session):
    user = await _make_user(db_session, "WeeklyLimited", points_balance=1000)
    item = await _make_item(db_session, name="Limited Weekly Item", cost=10, weekly_limit_per_user=2)

    r1 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r1.status_code == 201
    r2 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r2.status_code == 201
    r3 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r3.status_code == 409

    await db_session.refresh(user)
    assert user.points_balance == 980  # only the first two redemptions deducted


async def test_weekly_limit_cancelled_redemption_does_not_count(client, db_session):
    admin = await _make_user(db_session, "WeeklyLimitAdmin", role="admin")
    user = await _make_user(db_session, "WeeklyLimitCancelUser", points_balance=1000)
    item = await _make_item(db_session, name="Cancel-Exempt Item", cost=10, weekly_limit_per_user=1)

    r1 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r1.status_code == 201
    redemption_id = r1.json()["id"]

    cancel_r = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/cancel", json={}, headers=_bearer(admin))
    assert cancel_r.status_code == 200

    # Limit is 1/week; the only redemption so far was cancelled, so a fresh one should
    # be allowed again immediately, without waiting out the 7-day window.
    r2 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r2.status_code == 201


async def test_weekly_limit_resets_after_seven_days(client, db_session):
    user = await _make_user(db_session, "WeeklyLimitResetUser", points_balance=1000)
    item = await _make_item(db_session, name="Reset Item", cost=10, weekly_limit_per_user=1)

    r1 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r1.status_code == 201

    # Backdate the redemption past the trailing-7-day window so the limit should no
    # longer apply to it.
    redemption = (await db_session.execute(select(ShopRedemption).where(ShopRedemption.user_id == user.id))).scalar_one()
    redemption.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
    await db_session.commit()

    r2 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r2.status_code == 201


async def test_weekly_limit_none_is_unaffected(client, db_session):
    user = await _make_user(db_session, "NoWeeklyLimitUser", points_balance=1000)
    item = await _make_item(db_session, name="Unlimited Weekly Item", cost=10, weekly_limit_per_user=None)

    for _ in range(5):
        r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
        assert r.status_code == 201


async def test_weekly_remaining_surfaced_on_public_listing(client, db_session):
    user = await _make_user(db_session, "WeeklyRemainingUser", points_balance=1000)
    item = await _make_item(db_session, name="Remaining Item", cost=10, weekly_limit_per_user=3)

    r1 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r1.status_code == 201

    r = await client.get("/api/shop/items", headers=_bearer(user))
    assert r.status_code == 200
    out = next(i for i in r.json() if i["id"] == item.id)
    assert out["weekly_limit_per_user"] == 3
    assert out["weekly_remaining"] == 2


# ─── Own-ledger `type` filter (GET /api/points/transactions/me) ───────────────

async def test_ledger_type_filter_earned_vs_spent(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76500000000000401"
    user = await _make_user(db_session, "TypeFilterUser", steam_id=steam_id, points_balance=0)

    r1 = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": steam_id, "character_name": "TypeFilterUser", "session_seconds": 600},
        headers=_hdr(),
    )
    assert r1.status_code == 200  # reason="playtime", +10

    item = await _make_item(db_session, name="Type Filter Item", cost=5)
    r2 = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    assert r2.status_code == 201  # reason="redeem", -5

    r_earned = await client.get("/api/points/transactions/me?type=earned", headers=_bearer(user))
    assert r_earned.status_code == 200
    earned_items = r_earned.json()["items"]
    assert len(earned_items) == 1
    assert earned_items[0]["reason"] == "playtime"

    r_spent = await client.get("/api/points/transactions/me?type=spent", headers=_bearer(user))
    assert r_spent.status_code == 200
    spent_items = r_spent.json()["items"]
    assert len(spent_items) == 1
    assert spent_items[0]["reason"] == "redeem"

    r_all = await client.get("/api/points/transactions/me", headers=_bearer(user))
    assert r_all.json()["total"] == 2


async def test_ledger_type_filter_gifted_catches_admin_grant_reasons(client, db_session):
    admin = await _make_user(db_session, "TypeFilterAdmin", role="admin")
    user = await _make_user(db_session, "TypeFilterGiftedUser", points_balance=0)

    r = await client.post(
        "/api/admin/points/grant",
        json={"user_id": user.id, "delta": 50, "reason": "donation", "note": "birthday"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201

    r_gifted = await client.get("/api/points/transactions/me?type=gifted", headers=_bearer(user))
    assert r_gifted.status_code == 200
    items = r_gifted.json()["items"]
    assert len(items) == 1
    assert items[0]["reason"] == "donation"

    r_earned = await client.get("/api/points/transactions/me?type=earned", headers=_bearer(user))
    assert r_earned.json()["items"] == []


async def test_ledger_type_filter_refund_bucket(client, db_session):
    admin = await _make_user(db_session, "TypeFilterRefundAdmin", role="admin")
    user = await _make_user(db_session, "TypeFilterRefundUser", points_balance=100)
    item = await _make_item(db_session, name="Refund Filter Item", cost=20)
    redeem_r = await client.post("/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(user))
    redemption_id = redeem_r.json()["id"]

    cancel_r = await client.post(f"/api/admin/shop/redemptions/{redemption_id}/cancel", headers=_bearer(admin))
    assert cancel_r.status_code == 200

    r = await client.get("/api/points/transactions/me?type=refund", headers=_bearer(user))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["reason"] == "refund"
    assert items[0]["delta"] == 20


async def test_ledger_type_filter_invalid_value_400s(client, db_session):
    user = await _make_user(db_session, "TypeFilterInvalidUser")
    r = await client.get("/api/points/transactions/me?type=bogus", headers=_bearer(user))
    assert r.status_code == 400


# ─── Self-service CSV export (GET /api/points/transactions/export) ────────────

async def test_self_export_requires_login(client, db_session):
    r = await client.get("/api/points/transactions/export")
    assert r.status_code == 401


async def test_self_export_returns_only_own_csv_rows(client, db_session):
    user_a = await _make_user(db_session, "SelfExportAlice", points_balance=10)
    user_b = await _make_user(db_session, "SelfExportBob", points_balance=10)
    db_session.add(PointsTransaction(user_id=user_a.id, delta=10, balance_after=10, reason="playtime", detail="alice-only"))
    db_session.add(PointsTransaction(user_id=user_b.id, delta=999, balance_after=999, reason="donation", detail="bob-only"))
    await db_session.commit()

    r = await client.get("/api/points/transactions/export", headers=_bearer(user_a))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=my_points_history.csv" in r.headers["content-disposition"]

    body = r.text
    lines = body.strip().splitlines()
    assert lines[0] == "date,delta,balance_after,reason,detail"
    assert len(lines) == 2
    assert "alice-only" in body
    assert "bob-only" not in body


async def test_self_export_respects_type_filter(client, db_session):
    user = await _make_user(db_session, "SelfExportFilterUser", points_balance=10)
    db_session.add(PointsTransaction(user_id=user.id, delta=10, balance_after=10, reason="playtime"))
    db_session.add(PointsTransaction(user_id=user.id, delta=-5, balance_after=5, reason="redeem"))
    await db_session.commit()

    r = await client.get("/api/points/transactions/export?type=spent", headers=_bearer(user))
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert len(lines) == 2  # header + 1 "redeem" row
    assert "redeem" in lines[1]


# ─── Rolling net earn-rate (GET /api/points/earn-rate/me) ─────────────────────

async def test_earn_rate_requires_login(client, db_session):
    r = await client.get("/api/points/earn-rate/me")
    assert r.status_code == 401


async def test_earn_rate_computes_net_average_over_window(client, db_session):
    user = await _make_user(db_session, "EarnRateUser", points_balance=0)
    db_session.add(PointsTransaction(user_id=user.id, delta=100, balance_after=100, reason="playtime"))
    db_session.add(PointsTransaction(user_id=user.id, delta=-40, balance_after=60, reason="redeem"))
    await db_session.commit()

    r = await client.get("/api/points/earn-rate/me?days=10", headers=_bearer(user))
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 10
    assert body["net_delta"] == 60
    assert body["avg_per_day"] == 6.0


async def test_earn_rate_excludes_transactions_outside_window(client, db_session):
    user = await _make_user(db_session, "EarnRateOldTxUser", points_balance=0)
    old_tx = PointsTransaction(user_id=user.id, delta=500, balance_after=500, reason="playtime")
    db_session.add(old_tx)
    await db_session.commit()
    await db_session.refresh(old_tx)
    old_tx.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    await db_session.commit()

    r = await client.get("/api/points/earn-rate/me?days=30", headers=_bearer(user))
    assert r.status_code == 200
    body = r.json()
    assert body["net_delta"] == 0
    assert body["avg_per_day"] == 0.0


async def test_earn_rate_isolated_per_user(client, db_session):
    user_a = await _make_user(db_session, "EarnRateAlice", points_balance=0)
    user_b = await _make_user(db_session, "EarnRateBob", points_balance=0)
    db_session.add(PointsTransaction(user_id=user_a.id, delta=10, balance_after=10, reason="playtime"))
    db_session.add(PointsTransaction(user_id=user_b.id, delta=1000, balance_after=1000, reason="donation"))
    await db_session.commit()

    r = await client.get("/api/points/earn-rate/me", headers=_bearer(user_a))
    assert r.json()["net_delta"] == 10
