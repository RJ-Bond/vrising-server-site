"""Regression tests for the scheduled/recurring announcements system:
admin CRUD under /api/admin/announcements and the plugin-facing polling endpoint
GET /api/plugin/announcements (which replaced the old single-text
GET /api/plugin/announcement — see test_plugin_heartbeat.py, whose 3 tests for that
retired endpoint were removed alongside this file being added)."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import Announcement, Setting, User

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def _make_admin(db_session, username="AdminUser"):
    admin = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("adminpass1"),
        role="admin",
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    return admin


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


# ─── Admin CRUD ─────────────────────────────────────────────────────────────

async def test_list_announcements_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/announcements")
    assert r.status_code == 401


async def test_create_announcement_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/announcements", json={"text": "hello"})
    assert r.status_code == 401


async def test_create_list_update_delete_round_trip(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    create_r = await client.post(
        "/api/admin/announcements",
        json={"text": "Добро пожаловать!", "interval_minutes": 30},
        headers=headers,
    )
    assert create_r.status_code == 201
    created = create_r.json()
    assert created["text"] == "Добро пожаловать!"
    assert created["interval_minutes"] == 30
    assert created["enabled"] is True
    assert created["last_sent_at"] is None
    ann_id = created["id"]

    list_r = await client.get("/api/admin/announcements", headers=headers)
    assert list_r.status_code == 200
    items = list_r.json()
    assert len(items) == 1
    assert items[0]["id"] == ann_id

    update_r = await client.put(
        f"/api/admin/announcements/{ann_id}",
        json={"text": "Обновлённый текст", "enabled": False},
        headers=headers,
    )
    assert update_r.status_code == 200
    updated = update_r.json()
    assert updated["text"] == "Обновлённый текст"
    assert updated["enabled"] is False
    assert updated["interval_minutes"] == 30  # untouched field survives partial update

    delete_r = await client.delete(f"/api/admin/announcements/{ann_id}", headers=headers)
    assert delete_r.status_code == 204

    list_r2 = await client.get("/api/admin/announcements", headers=headers)
    assert list_r2.json() == []


async def test_create_announcement_rejects_empty_text(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post("/api/admin/announcements", json={"text": "   "}, headers=_bearer(admin))
    assert r.status_code == 422


async def test_create_announcement_rejects_nonpositive_interval(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post(
        "/api/admin/announcements",
        json={"text": "hi", "interval_minutes": 0},
        headers=_bearer(admin),
    )
    assert r.status_code == 422


async def test_send_now_resets_last_sent_at_to_null(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)
    a = Announcement(text="Раз в час", interval_minutes=60, last_sent_at=datetime.now(timezone.utc))
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)

    r = await client.post(f"/api/admin/announcements/{a.id}/send-now", headers=headers)
    assert r.status_code == 200
    assert r.json()["last_sent_at"] is None


async def test_send_now_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/announcements/1/send-now")
    assert r.status_code == 401


# ─── Plugin-facing polling endpoint ────────────────────────────────────────

async def test_plugin_announcements_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/announcements")
    assert r.status_code == 401


async def test_fresh_once_announcement_is_due_then_not_due_again(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Announcement(text="Разовое объявление", interval_minutes=None))
    await db_session.commit()

    r1 = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r1.status_code == 200
    assert r1.json()["announcements"] == [{"text": "Разовое объявление", "target_steam_id": None}]

    r2 = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r2.status_code == 200
    assert r2.json()["announcements"] == []


async def test_recurring_announcement_due_after_interval_elapses(client, db_session):
    await _set_plugin_key(db_session)
    a = Announcement(
        text="Повторяющееся",
        interval_minutes=30,
        last_sent_at=datetime.now(timezone.utc) - timedelta(minutes=40),
    )
    db_session.add(a)
    await db_session.commit()

    r = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["announcements"] == [{"text": "Повторяющееся", "target_steam_id": None}]


async def test_recurring_announcement_not_due_before_interval_elapses(client, db_session):
    await _set_plugin_key(db_session)
    a = Announcement(
        text="Ещё рано",
        interval_minutes=30,
        last_sent_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(a)
    await db_session.commit()

    r = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["announcements"] == []


async def test_disabled_announcement_is_never_due(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Announcement(text="Отключено", interval_minutes=None, enabled=False))
    await db_session.commit()

    r = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["announcements"] == []


async def test_expired_announcement_is_never_due(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Announcement(
        text="Истекло",
        interval_minutes=None,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    await db_session.commit()

    r = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["announcements"] == []


async def test_multiple_due_announcements_returned_in_created_order(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Announcement(text="Первое"))
    await db_session.commit()
    db_session.add(Announcement(text="Второе"))
    await db_session.commit()

    r = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["announcements"] == [
        {"text": "Первое", "target_steam_id": None},
        {"text": "Второе", "target_steam_id": None},
    ]


# ─── Test-send (self-only "Проверить в игре") ──────────────────────────────

async def test_test_send_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/announcements/test-send", json={"text": "hi"})
    assert r.status_code == 401


async def test_test_send_requires_steam_id_linked(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post(
        "/api/admin/announcements/test-send",
        json={"text": "Проверка"},
        headers=_bearer(admin),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "steam_id_not_linked"


async def test_test_send_creates_targeted_row_due_on_next_poll(client, db_session):
    admin = await _make_admin(db_session)
    admin.steam_id = "76561198000000000"
    await db_session.commit()
    await _set_plugin_key(db_session)

    r = await client.post(
        "/api/admin/announcements/test-send",
        json={"text": "Проверка в игре"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    created = r.json()
    assert created["text"] == "Проверка в игре"
    assert created["target_steam_id"] == "76561198000000000"
    assert created["last_sent_at"] is None

    poll_r = await client.get("/api/plugin/announcements", headers=_hdr())
    assert poll_r.status_code == 200
    assert poll_r.json()["announcements"] == [
        {"text": "Проверка в игре", "target_steam_id": "76561198000000000"}
    ]


async def test_test_send_row_excluded_from_admin_list(client, db_session):
    admin = await _make_admin(db_session)
    admin.steam_id = "76561198000000001"
    await db_session.commit()

    r = await client.post(
        "/api/admin/announcements/test-send",
        json={"text": "Скрытое тестовое"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201

    list_r = await client.get("/api/admin/announcements", headers=_bearer(admin))
    assert list_r.status_code == 200
    assert list_r.json() == []


# ─── Per-server scoping (server_num) ────────────────────────────────────────

async def test_create_announcement_defaults_server_num_to_1(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post("/api/admin/announcements", json={"text": "hi"}, headers=_bearer(admin))
    assert r.status_code == 201
    assert r.json()["server_num"] == 1


async def test_create_announcement_with_explicit_server_num(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post(
        "/api/admin/announcements",
        json={"text": "Только для сервера 2", "server_num": 2},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    assert r.json()["server_num"] == 2


async def test_admin_list_announcements_filtered_by_server_num(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)
    await client.post("/api/admin/announcements", json={"text": "Сервер 1", "server_num": 1}, headers=headers)
    await client.post("/api/admin/announcements", json={"text": "Сервер 2", "server_num": 2}, headers=headers)

    r1 = await client.get("/api/admin/announcements?server_num=1", headers=headers)
    assert [a["text"] for a in r1.json()] == ["Сервер 1"]

    r2 = await client.get("/api/admin/announcements?server_num=2", headers=headers)
    assert [a["text"] for a in r2.json()] == ["Сервер 2"]

    # Omitting server_num returns everything (backward compat).
    r_all = await client.get("/api/admin/announcements", headers=headers)
    assert len(r_all.json()) == 2


async def test_plugin_announcements_scoped_to_server_num(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Announcement(text="Для сервера 1", interval_minutes=None, server_num=1))
    db_session.add(Announcement(text="Для сервера 2", interval_minutes=None, server_num=2))
    await db_session.commit()

    r1 = await client.get("/api/plugin/announcements?server_num=1", headers=_hdr())
    assert r1.status_code == 200
    assert r1.json()["announcements"] == [{"text": "Для сервера 1", "target_steam_id": None}]

    r2 = await client.get("/api/plugin/announcements?server_num=2", headers=_hdr())
    assert r2.status_code == 200
    assert r2.json()["announcements"] == [{"text": "Для сервера 2", "target_steam_id": None}]


async def test_plugin_announcements_defaults_server_num_to_1(client, db_session):
    """An old plugin build that never sends server_num should still get server 1's
    announcements (and never see a server_num=2-only announcement)."""
    await _set_plugin_key(db_session)
    db_session.add(Announcement(text="Для сервера 1", interval_minutes=None, server_num=1))
    db_session.add(Announcement(text="Для сервера 2", interval_minutes=None, server_num=2))
    await db_session.commit()

    r = await client.get("/api/plugin/announcements", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["announcements"] == [{"text": "Для сервера 1", "target_steam_id": None}]


async def test_update_announcement_can_move_to_different_server(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)
    create_r = await client.post("/api/admin/announcements", json={"text": "Перемещаемое"}, headers=headers)
    ann_id = create_r.json()["id"]
    assert create_r.json()["server_num"] == 1

    update_r = await client.put(
        f"/api/admin/announcements/{ann_id}",
        json={"server_num": 2},
        headers=headers,
    )
    assert update_r.status_code == 200
    assert update_r.json()["server_num"] == 2


async def test_test_send_targets_specific_server(client, db_session):
    admin = await _make_admin(db_session)
    admin.steam_id = "76561198000000002"
    await db_session.commit()
    await _set_plugin_key(db_session)

    r = await client.post(
        "/api/admin/announcements/test-send",
        json={"text": "Проверка на сервере 2", "server_num": 2},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    assert r.json()["server_num"] == 2

    poll_r1 = await client.get("/api/plugin/announcements?server_num=1", headers=_hdr())
    assert poll_r1.json()["announcements"] == []

    poll_r2 = await client.get("/api/plugin/announcements?server_num=2", headers=_hdr())
    assert poll_r2.json()["announcements"] == [
        {"text": "Проверка на сервере 2", "target_steam_id": "76561198000000002"}
    ]
