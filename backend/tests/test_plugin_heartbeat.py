"""Regression tests for the BepInEx plugin heartbeat endpoints:
POST /api/plugin/heartbeat (gated by the shared plugin_api_key secret) and
GET /api/admin/plugin-status (admin-only, reads back the stored heartbeats)."""
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


async def test_heartbeat_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/heartbeat", json={"server_num": 1})
    assert r.status_code == 401


async def test_heartbeat_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/heartbeat",
        json={"server_num": 1},
        headers=_hdr("not-the-real-key"),
    )
    assert r.status_code == 401


async def test_heartbeat_creates_a_row(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/heartbeat",
        json={"server_num": 1, "server_name": "PvP Server", "plugin_version": "1.0.0", "player_count": 5},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["success"] is True

    admin = await _make_admin(db_session)
    status_r = await client.get("/api/admin/plugin-status", headers=_bearer(admin))
    assert status_r.status_code == 200
    rows = status_r.json()
    assert len(rows) == 1
    assert rows[0]["server_num"] == 1
    assert rows[0]["server_name"] == "PvP Server"
    assert rows[0]["plugin_version"] == "1.0.0"
    assert rows[0]["player_count"] == 5
    assert rows[0]["last_seen_at"] is not None


async def test_second_heartbeat_updates_rather_than_duplicates(client, db_session):
    await _set_plugin_key(db_session)
    await client.post(
        "/api/plugin/heartbeat",
        json={"server_num": 1, "server_name": "PvP Server", "plugin_version": "1.0.0", "player_count": 5},
        headers=_hdr(),
    )
    r = await client.post(
        "/api/plugin/heartbeat",
        json={"server_num": 1, "server_name": "PvP Server", "plugin_version": "1.0.1", "player_count": 8},
        headers=_hdr(),
    )
    assert r.status_code == 200

    admin = await _make_admin(db_session, username="AdminUser2")
    status_r = await client.get("/api/admin/plugin-status", headers=_bearer(admin))
    assert status_r.status_code == 200
    rows = status_r.json()
    assert len(rows) == 1
    assert rows[0]["server_num"] == 1
    assert rows[0]["plugin_version"] == "1.0.1"
    assert rows[0]["player_count"] == 8


async def test_plugin_status_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/plugin-status")
    assert r.status_code == 401


async def test_plugin_status_returns_stored_data_per_server(client, db_session):
    await _set_plugin_key(db_session)
    await client.post(
        "/api/plugin/heartbeat",
        json={"server_num": 2, "server_name": "PvE Server", "plugin_version": "2.3.1", "player_count": 12},
        headers=_hdr(),
    )

    admin = await _make_admin(db_session, username="AdminUser3")
    status_r = await client.get("/api/admin/plugin-status", headers=_bearer(admin))
    assert status_r.status_code == 200
    rows = status_r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["server_num"] == 2
    assert row["server_name"] == "PvE Server"
    assert row["plugin_version"] == "2.3.1"
    assert row["player_count"] == 12
    assert row["last_seen_at"] is not None
