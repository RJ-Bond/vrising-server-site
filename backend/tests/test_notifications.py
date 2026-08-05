"""Regression tests for backend/routers/notifications.py: listing a user's own
notifications with unread count, marking all read, and deleting a notification
(with ownership enforced — deleting someone else's notification is a silent
no-op, matching the endpoint's design). See the Notification model in models.py."""
import json

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import Notification, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _make_notification(db_session, user, ntype="reply", data=None, read=False):
    n = Notification(
        user_id=user.id,
        type=ntype,
        data=json.dumps(data or {"comment_id": 1, "news_slug": "abc", "from_username": "Someone"}),
        read=read,
    )
    db_session.add(n)
    await db_session.commit()
    await db_session.refresh(n)
    return n


# ─── GET /api/notifications ───────────────────────────────────────────────────

async def test_get_notifications_requires_login(client, db_session):
    r = await client.get("/api/notifications")
    assert r.status_code == 401


async def test_get_notifications_returns_own_items_with_unread_count(client, db_session):
    user = await _make_user(db_session, "NotifUser1")
    other = await _make_user(db_session, "NotifUser2")
    await _make_notification(db_session, user, read=False)
    await _make_notification(db_session, user, read=True)
    await _make_notification(db_session, other, read=False)  # someone else's — must not leak

    r = await client.get("/api/notifications", headers=_bearer(user))
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["unread"] == 1
    for item in body["items"]:
        assert isinstance(item["data"], dict)


async def test_get_notifications_orders_newest_first(client, db_session):
    user = await _make_user(db_session, "NotifUser3")
    first = await _make_notification(db_session, user, data={"seq": 1})
    second = await _make_notification(db_session, user, data={"seq": 2})

    r = await client.get("/api/notifications", headers=_bearer(user))
    assert r.status_code == 200
    ids = [i["id"] for i in r.json()["items"]]
    assert ids[0] == second.id
    assert ids[1] == first.id


# ─── POST /api/notifications/read-all ────────────────────────────────────────

async def test_mark_all_read_only_affects_own_unread(client, db_session):
    user = await _make_user(db_session, "NotifUser4")
    other = await _make_user(db_session, "NotifUser5")
    await _make_notification(db_session, user, read=False)
    await _make_notification(db_session, user, read=False)
    other_notif = await _make_notification(db_session, other, read=False)

    r = await client.post("/api/notifications/read-all", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    rows = (await db_session.execute(select(Notification).where(Notification.user_id == user.id))).scalars().all()
    assert all(n.read for n in rows)

    await db_session.refresh(other_notif)
    assert other_notif.read is False


async def test_mark_all_read_requires_login(client, db_session):
    r = await client.post("/api/notifications/read-all")
    assert r.status_code == 401


# ─── DELETE /api/notifications/{id} ──────────────────────────────────────────

async def test_delete_own_notification_removes_it(client, db_session):
    user = await _make_user(db_session, "NotifUser6")
    n = await _make_notification(db_session, user)

    r = await client.delete(f"/api/notifications/{n.id}", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # `n` is already in db_session's identity map (loaded by _make_notification above) —
    # db_session.get() would return that cached instance without re-querying. Force a
    # real reload via select() to see what the request's own session actually committed.
    remaining = (await db_session.execute(select(Notification).where(Notification.id == n.id))).scalar_one_or_none()
    assert remaining is None


async def test_delete_other_users_notification_is_noop(client, db_session):
    owner = await _make_user(db_session, "NotifUser7")
    intruder = await _make_user(db_session, "NotifUser8")
    n = await _make_notification(db_session, owner)

    r = await client.delete(f"/api/notifications/{n.id}", headers=_bearer(intruder))
    assert r.status_code == 200  # endpoint always returns {"ok": True}, no leak of existence

    still_there = await db_session.get(Notification, n.id)
    assert still_there is not None


async def test_delete_nonexistent_notification_is_noop(client, db_session):
    user = await _make_user(db_session, "NotifUser9")
    r = await client.delete("/api/notifications/999999", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_delete_notification_requires_login(client, db_session):
    r = await client.delete("/api/notifications/1")
    assert r.status_code == 401
