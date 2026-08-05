"""Regression tests for Web Push: the subscribe/unsubscribe endpoints in
backend/routers/notifications.py, and send_push() in backend/helpers.py — in
particular that a misconfigured/unset VAPID key, a webpush() failure, or an
expired (410) subscription never propagates as an exception to the caller (every
call site fires send_push() via `asyncio.create_task`, so callers as a whole rely
on this — see e.g. POST /api/messages in backend/routers/messages.py)."""
import pytest

import backend.helpers as helpers
import backend.routers.notifications as notifications_module
from backend.auth import create_access_token, get_password_hash
from backend.models import PushSubscription, User

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


# ─── GET /api/push/vapid-public-key ──────────────────────────────────────────

async def test_vapid_public_key_reflects_configured_value(client, db_session, monkeypatch):
    monkeypatch.setattr(notifications_module, "VAPID_PUBLIC_KEY", "test-public-key")
    r = await client.get("/api/push/vapid-public-key")
    assert r.status_code == 200
    assert r.json() == {"public_key": "test-public-key"}


async def test_vapid_public_key_empty_when_unconfigured(client, db_session, monkeypatch):
    monkeypatch.setattr(notifications_module, "VAPID_PUBLIC_KEY", "")
    r = await client.get("/api/push/vapid-public-key")
    assert r.status_code == 200
    assert r.json() == {"public_key": ""}


# ─── POST /api/push/subscribe ────────────────────────────────────────────────

async def test_subscribe_requires_login(client, db_session):
    r = await client.post("/api/push/subscribe", json={"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}})
    assert r.status_code == 401


async def test_subscribe_creates_row(client, db_session):
    user = await _make_user(db_session, "PushUser1")
    r = await client.post(
        "/api/push/subscribe",
        headers=_bearer(user),
        json={"endpoint": "https://push.example/1", "keys": {"p256dh": "pkey", "auth": "akey"}},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    from sqlalchemy import select
    rows = (await db_session.execute(select(PushSubscription).where(PushSubscription.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].endpoint == "https://push.example/1"
    assert rows[0].p256dh == "pkey"
    assert rows[0].auth == "akey"


async def test_subscribe_upserts_by_endpoint(client, db_session):
    """Re-subscribing the same endpoint (e.g. re-registering after clearing/re-granting
    permission) updates the existing row instead of erroring on the unique constraint,
    and re-pointing that endpoint at a different account re-assigns ownership rather
    than leaving two rows racing for the same endpoint."""
    user1 = await _make_user(db_session, "PushUser2")
    user2 = await _make_user(db_session, "PushUser3")
    endpoint = "https://push.example/shared-device"

    r1 = await client.post("/api/push/subscribe", headers=_bearer(user1), json={"endpoint": endpoint, "keys": {"p256dh": "p1", "auth": "a1"}})
    assert r1.status_code == 200
    r2 = await client.post("/api/push/subscribe", headers=_bearer(user2), json={"endpoint": endpoint, "keys": {"p256dh": "p2", "auth": "a2"}})
    assert r2.status_code == 200

    from sqlalchemy import select
    rows = (await db_session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == user2.id
    assert rows[0].p256dh == "p2"


# ─── DELETE /api/push/subscribe ──────────────────────────────────────────────

async def test_unsubscribe_requires_login(client, db_session):
    r = await client.request("DELETE", "/api/push/subscribe", json={"endpoint": "https://push.example/1"})
    assert r.status_code == 401


async def test_unsubscribe_removes_own_subscription(client, db_session):
    user = await _make_user(db_session, "PushUser4")
    await client.post("/api/push/subscribe", headers=_bearer(user), json={"endpoint": "https://push.example/2", "keys": {"p256dh": "p", "auth": "a"}})

    r = await client.request("DELETE", "/api/push/subscribe", headers=_bearer(user), json={"endpoint": "https://push.example/2"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    from sqlalchemy import select
    rows = (await db_session.execute(select(PushSubscription).where(PushSubscription.user_id == user.id))).scalars().all()
    assert rows == []


async def test_unsubscribe_other_users_subscription_is_noop(client, db_session):
    owner = await _make_user(db_session, "PushUser5")
    intruder = await _make_user(db_session, "PushUser6")
    await client.post("/api/push/subscribe", headers=_bearer(owner), json={"endpoint": "https://push.example/3", "keys": {"p256dh": "p", "auth": "a"}})

    r = await client.request("DELETE", "/api/push/subscribe", headers=_bearer(intruder), json={"endpoint": "https://push.example/3"})
    assert r.status_code == 200  # no leak of existence/ownership, same shape as DELETE /api/notifications/{id}

    from sqlalchemy import select
    rows = (await db_session.execute(select(PushSubscription).where(PushSubscription.endpoint == "https://push.example/3"))).scalars().all()
    assert len(rows) == 1  # untouched


# ─── send_push() ──────────────────────────────────────────────────────────────

async def test_send_push_noop_when_vapid_unconfigured(db_session, monkeypatch):
    """Default test env has no VAPID_PRIVATE_KEY set — send_push must silently no-op,
    not raise, matching every real deployment where the feature is simply off."""
    monkeypatch.setattr(helpers, "VAPID_PRIVATE_KEY", "")
    # Must not raise even though user_id doesn't exist and no subscriptions exist.
    await helpers.send_push(999999, "title", "body", "/")


async def test_send_push_swallows_webpush_exception(db_session, monkeypatch):
    """A generic webpush() failure (network error, bad payload, whatever) must be
    logged and swallowed — a push delivery failure must never surface as an error to
    whatever request path triggered it (e.g. posting a comment reply)."""
    user = await _make_user(db_session, "PushUser7")
    db_session.add(PushSubscription(user_id=user.id, endpoint="https://push.example/fail", p256dh="p", auth="a"))
    await db_session.commit()

    monkeypatch.setattr(helpers, "VAPID_PRIVATE_KEY", "fake-private-key")

    def _boom(*args, **kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(helpers, "webpush", _boom)

    # Must not raise.
    await helpers.send_push(user.id, "title", "body", "/")


async def test_send_push_prunes_stale_subscription_on_410(db_session, monkeypatch):
    """A push service reporting 404/410 means the subscription is dead (browser
    unregistered it, user cleared site data, etc.) — send_push should delete the row
    so it isn't retried forever, without raising."""
    from pywebpush import WebPushException

    user = await _make_user(db_session, "PushUser8")
    db_session.add(PushSubscription(user_id=user.id, endpoint="https://push.example/gone", p256dh="p", auth="a"))
    await db_session.commit()

    monkeypatch.setattr(helpers, "VAPID_PRIVATE_KEY", "fake-private-key")

    class _FakeResponse:
        status_code = 410

    def _gone(*args, **kwargs):
        raise WebPushException("gone", response=_FakeResponse())

    monkeypatch.setattr(helpers, "webpush", _gone)

    await helpers.send_push(user.id, "title", "body", "/")

    from sqlalchemy import select
    rows = (await db_session.execute(select(PushSubscription).where(PushSubscription.user_id == user.id))).scalars().all()
    assert rows == []


async def test_send_push_noop_when_user_has_no_subscriptions(db_session, monkeypatch):
    user = await _make_user(db_session, "PushUser9")
    monkeypatch.setattr(helpers, "VAPID_PRIVATE_KEY", "fake-private-key")

    called = False

    def _should_not_run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(helpers, "webpush", _should_not_run)

    await helpers.send_push(user.id, "title", "body", "/")
    assert called is False
