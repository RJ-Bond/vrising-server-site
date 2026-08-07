"""Regression tests for the write-time hooks that call helpers.activity_broadcast()
(see that function's docstring) — POST/PUT /api/admin/news (publish transition only),
POST /api/admin/events, and POST /api/admin/shop/redemptions/{id}/fulfill. Each test
registers a fake queue directly in helpers._activity_sse_clients (the same registration
an /api/activity-feed/stream connection performs) and asserts the real endpoint's
side effect actually reaches it — a broken or missing activity_broadcast() call would
otherwise only be caught by noticing the homepage feed never updates in production."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.helpers import _activity_sse_clients
from backend.models import ShopItem, User

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


@pytest.fixture
def activity_queue():
    """Registers a fresh queue in _activity_sse_clients for the test body, and always
    discards it afterward so one test's leftover queue can't affect another (broadcasts
    from unrelated tests running in the same session would otherwise pile up in it)."""
    q: asyncio.Queue = asyncio.Queue(maxsize=20)
    _activity_sse_clients.add(q)
    try:
        yield q
    finally:
        _activity_sse_clients.discard(q)


async def _next_item(q):
    raw = await asyncio.wait_for(q.get(), timeout=5)
    return json.loads(raw)


async def test_create_published_news_broadcasts_item(client, db_session, activity_queue):
    admin = await _make_user(db_session, "NewsBroadcastAdmin", role="admin")
    r = await client.post(
        "/api/admin/news",
        json={"title": "Обновление сервера", "summary": "Кратко", "content": "Текст", "published": True},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    item = await _next_item(activity_queue)
    assert item["type"] == "news"
    assert item["title"] == "Обновление сервера"


async def test_create_draft_news_does_not_broadcast(client, db_session, activity_queue):
    admin = await _make_user(db_session, "NewsDraftAdmin", role="admin")
    r = await client.post(
        "/api/admin/news",
        json={"title": "Черновик", "summary": "", "content": "Текст", "published": False},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    assert activity_queue.empty()


async def test_update_news_to_published_broadcasts_item(client, db_session, activity_queue):
    admin = await _make_user(db_session, "NewsUpdateAdmin", role="admin")
    create_r = await client.post(
        "/api/admin/news",
        json={"title": "Позже опубликуем", "summary": "", "content": "Текст", "published": False},
        headers=_bearer(admin),
    )
    news_id = create_r.json()["id"]
    assert activity_queue.empty()  # draft create must not have broadcast

    r = await client.put(
        f"/api/admin/news/{news_id}",
        json={"published": True},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    item = await _next_item(activity_queue)
    assert item["type"] == "news"
    assert item["title"] == "Позже опубликуем"


async def test_update_already_published_news_does_not_rebroadcast(client, db_session, activity_queue):
    """Editing an already-published article (e.g. fixing a typo) is not a new event —
    only the false->true publish transition should broadcast."""
    admin = await _make_user(db_session, "NewsReeditAdmin", role="admin")
    create_r = await client.post(
        "/api/admin/news",
        json={"title": "Уже опубликовано", "summary": "", "content": "Текст", "published": True},
        headers=_bearer(admin),
    )
    news_id = create_r.json()["id"]
    await _next_item(activity_queue)  # drain the create-time broadcast

    r = await client.put(
        f"/api/admin/news/{news_id}",
        json={"summary": "Исправлена опечатка"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    assert activity_queue.empty()


async def test_admin_create_event_broadcasts_item(client, db_session, activity_queue):
    admin = await _make_user(db_session, "EventBroadcastAdmin", role="admin")
    r = await client.post(
        "/api/admin/events",
        json={
            "title": "Турнир Кровавой Луны", "description": "desc", "event_type": "pvp",
            "start_date": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    item = await _next_item(activity_queue)
    assert item["type"] == "event"
    assert item["title"] == "Турнир Кровавой Луны"


async def test_fulfill_shop_redemption_broadcasts_item(client, db_session, activity_queue):
    admin = await _make_user(db_session, "ShopBroadcastAdmin", role="admin")
    buyer = await _make_user(db_session, "ShopBroadcastBuyer", role="user")
    buyer.points_balance = 100
    await db_session.commit()

    item = ShopItem(name="Waypoint Shard", cost=30, is_active=True)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)

    redeem_r = await client.post(
        "/api/shop/redeem", json={"shop_item_id": item.id}, headers=_bearer(buyer),
    )
    assert redeem_r.status_code == 201
    redemption_id = redeem_r.json()["id"]
    assert activity_queue.empty()  # redeeming (pending) must not itself broadcast

    r = await client.post(
        f"/api/admin/shop/redemptions/{redemption_id}/fulfill", json={}, headers=_bearer(admin),
    )
    assert r.status_code == 200
    fed_item = await _next_item(activity_queue)
    assert fed_item["type"] == "shop_redemption"
    assert fed_item["title"] == "Waypoint Shard"
    assert "ShopBroadcastBuyer" in fed_item["subtitle"]
