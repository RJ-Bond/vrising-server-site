"""Regression tests for scheduled server restart (ScheduledRestart table):
- GET /api/plugin/restart-status, POST /api/plugin/schedule-restart,
  POST /api/plugin/cancel-restart (plugin-gated by X-Plugin-Key)
- GET/POST/DELETE /api/admin/servers/{server_num}/restart (admin-JWT-gated)

Both flavors share the same underlying row and _schedule_restart/_cancel_restart
helpers in main.py, so an in-game admin command and the site admin panel can't get
out of sync — see the "Scheduled server restart" section comments in main.py."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import Setting, User

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


def _parse_iso_z(s: str) -> datetime:
    assert s.endswith("Z"), f"expected an explicit Z-suffixed UTC timestamp, got {s!r}"
    return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)


# ─── Plugin-facing endpoints ────────────────────────────────────────────────

async def test_plugin_restart_status_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/restart-status")
    assert r.status_code == 401


async def test_plugin_restart_status_returns_null_when_nothing_scheduled(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/restart-status", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"restart_at": None}


async def test_plugin_schedule_restart_sets_correct_restart_at(client, db_session):
    await _set_plugin_key(db_session)
    before = datetime.now(timezone.utc)
    r = await client.post(
        "/api/plugin/schedule-restart",
        json={"server_num": 1, "minutes": 10},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    restart_at = _parse_iso_z(body["restart_at"])
    expected = before + timedelta(minutes=10)
    assert abs((restart_at - expected).total_seconds()) < 5

    status_r = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert status_r.status_code == 200
    assert status_r.json()["restart_at"] == body["restart_at"]


async def test_plugin_schedule_restart_rejects_zero_minutes(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/schedule-restart", json={"server_num": 1, "minutes": 0}, headers=_hdr()
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_minutes"


async def test_plugin_schedule_restart_rejects_negative_minutes(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/schedule-restart", json={"server_num": 1, "minutes": -5}, headers=_hdr()
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_minutes"


async def test_plugin_cancel_restart_clears_it(client, db_session):
    await _set_plugin_key(db_session)
    await client.post(
        "/api/plugin/schedule-restart", json={"server_num": 1, "minutes": 5}, headers=_hdr()
    )
    cancel_r = await client.post(
        "/api/plugin/cancel-restart", json={"server_num": 1}, headers=_hdr()
    )
    assert cancel_r.status_code == 200
    assert cancel_r.json() == {"success": True}

    status_r = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert status_r.json() == {"restart_at": None}


async def test_plugin_cancel_restart_is_noop_when_nothing_scheduled(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/cancel-restart", json={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"success": True}


async def test_plugin_restart_status_scoped_per_server(client, db_session):
    await _set_plugin_key(db_session)
    await client.post(
        "/api/plugin/schedule-restart", json={"server_num": 1, "minutes": 5}, headers=_hdr()
    )
    r2 = await client.get("/api/plugin/restart-status", params={"server_num": 2}, headers=_hdr())
    assert r2.json() == {"restart_at": None}
    r1 = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert r1.json()["restart_at"] is not None


# ─── Admin-facing endpoints ─────────────────────────────────────────────────

async def test_admin_get_restart_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/servers/1/restart")
    assert r.status_code == 401


async def test_admin_post_restart_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/servers/1/restart", json={"minutes": 5})
    assert r.status_code == 401


async def test_admin_delete_restart_requires_admin_auth(client, db_session):
    r = await client.delete("/api/admin/servers/1/restart")
    assert r.status_code == 401


async def test_admin_get_restart_returns_null_when_unset(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/servers/1/restart", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json() == {"restart_at": None}


async def test_admin_schedule_restart_rejects_invalid_minutes(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post("/api/admin/servers/1/restart", json={"minutes": 0}, headers=_bearer(admin))
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_minutes"


async def test_admin_schedule_get_cancel_roundtrip(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    post_r = await client.post("/api/admin/servers/1/restart", json={"minutes": 15}, headers=headers)
    assert post_r.status_code == 200
    restart_at = post_r.json()["restart_at"]
    assert restart_at is not None
    assert restart_at.endswith("Z")

    get_r = await client.get("/api/admin/servers/1/restart", headers=headers)
    assert get_r.status_code == 200
    assert get_r.json() == {"restart_at": restart_at}

    del_r = await client.delete("/api/admin/servers/1/restart", headers=headers)
    assert del_r.status_code == 200
    assert del_r.json() == {"success": True}

    get_r2 = await client.get("/api/admin/servers/1/restart", headers=headers)
    assert get_r2.json() == {"restart_at": None}


async def test_admin_restart_scoped_per_server(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    await client.post("/api/admin/servers/1/restart", json={"minutes": 5}, headers=headers)

    r2 = await client.get("/api/admin/servers/2/restart", headers=headers)
    assert r2.json() == {"restart_at": None}
    r1 = await client.get("/api/admin/servers/1/restart", headers=headers)
    assert r1.json()["restart_at"] is not None


# ─── Admin panel and plugin poll the same underlying row ───────────────────

async def test_admin_schedule_is_visible_to_plugin_poll(client, db_session):
    admin = await _make_admin(db_session)
    await _set_plugin_key(db_session)

    post_r = await client.post("/api/admin/servers/1/restart", json={"minutes": 7}, headers=_bearer(admin))
    restart_at = post_r.json()["restart_at"]

    plugin_r = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert plugin_r.json() == {"restart_at": restart_at}


async def test_plugin_cleanup_cancel_is_visible_to_admin_panel(client, db_session):
    admin = await _make_admin(db_session)
    await _set_plugin_key(db_session)

    await client.post(
        "/api/plugin/schedule-restart", json={"server_num": 1, "minutes": 3}, headers=_hdr()
    )
    await client.post("/api/plugin/cancel-restart", json={"server_num": 1}, headers=_hdr())

    admin_r = await client.get("/api/admin/servers/1/restart", headers=_bearer(admin))
    assert admin_r.json() == {"restart_at": None}
