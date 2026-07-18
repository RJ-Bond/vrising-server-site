"""Regression tests for the recurring daily restart schedule layered on top of the
one-off ScheduledRestart.restart_at:
- GET/POST/DELETE /api/admin/servers/{server_num}/daily-restart (admin-JWT-gated)
- The self-arming logic added to GET /api/plugin/restart-status: whenever restart_at is
  null and daily_restart_time is set for that server_num, the endpoint computes the next
  occurrence, persists it into restart_at, and returns it — independent of the existing
  one-off restart_at / cancel-restart behavior, which this must not disturb.

See ScheduledRestart.daily_restart_time's docstring in backend/models.py and the
"Recurring daily restart (admin)" / self-arming comments in backend/main.py."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import Setting, User

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-daily"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def _make_admin(db_session, username="DailyRestartAdmin"):
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


# ─── Admin endpoints ─────────────────────────────────────────────────────────

async def test_get_daily_restart_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/servers/1/daily-restart")
    assert r.status_code == 401


async def test_post_daily_restart_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/servers/1/daily-restart", json={"time": "06:00"})
    assert r.status_code == 401


async def test_delete_daily_restart_requires_admin_auth(client, db_session):
    r = await client.delete("/api/admin/servers/1/daily-restart")
    assert r.status_code == 401


async def test_get_daily_restart_returns_null_when_unset(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/servers/1/daily-restart", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json() == {"daily_restart_time": None}


async def test_set_get_clear_roundtrip(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    post_r = await client.post("/api/admin/servers/1/daily-restart", json={"time": "06:30"}, headers=headers)
    assert post_r.status_code == 200
    assert post_r.json() == {"daily_restart_time": "06:30"}

    get_r = await client.get("/api/admin/servers/1/daily-restart", headers=headers)
    assert get_r.status_code == 200
    assert get_r.json() == {"daily_restart_time": "06:30"}

    del_r = await client.delete("/api/admin/servers/1/daily-restart", headers=headers)
    assert del_r.status_code == 200
    assert del_r.json() == {"success": True}

    get_r2 = await client.get("/api/admin/servers/1/daily-restart", headers=headers)
    assert get_r2.json() == {"daily_restart_time": None}


@pytest.mark.parametrize("bad_time", ["9:00", "06:5", "25:00", "12:60", "abcd", "06-00", ""])
async def test_post_daily_restart_rejects_malformed_time(client, db_session, bad_time):
    admin = await _make_admin(db_session)
    r = await client.post(
        "/api/admin/servers/1/daily-restart", json={"time": bad_time}, headers=_bearer(admin)
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_time"


async def test_daily_restart_scoped_per_server(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)
    await client.post("/api/admin/servers/1/daily-restart", json={"time": "06:00"}, headers=headers)

    r2 = await client.get("/api/admin/servers/2/daily-restart", headers=headers)
    assert r2.json() == {"daily_restart_time": None}
    r1 = await client.get("/api/admin/servers/1/daily-restart", headers=headers)
    assert r1.json() == {"daily_restart_time": "06:00"}


# ─── Self-arming via GET /api/plugin/restart-status ─────────────────────────

async def test_restart_status_self_arms_from_daily_restart_time(client, db_session):
    admin = await _make_admin(db_session)
    await _set_plugin_key(db_session)

    await client.post("/api/admin/servers/1/daily-restart", json={"time": "06:00"}, headers=_bearer(admin))

    r = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    restart_at = r.json()["restart_at"]
    assert restart_at is not None
    parsed = _parse_iso_z(restart_at)
    now = datetime.now(timezone.utc)
    assert parsed > now
    assert parsed < now + timedelta(hours=24, minutes=1)


async def test_restart_status_is_stable_once_armed(client, db_session):
    """A second poll must not recompute/shift an already-armed restart_at."""
    admin = await _make_admin(db_session)
    await _set_plugin_key(db_session)
    await client.post("/api/admin/servers/1/daily-restart", json={"time": "06:00"}, headers=_bearer(admin))

    r1 = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    r2 = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert r1.json()["restart_at"] == r2.json()["restart_at"]


async def test_cancel_restart_does_not_clear_daily_restart_time_and_rearms(client, db_session):
    admin = await _make_admin(db_session)
    await _set_plugin_key(db_session)
    await client.post("/api/admin/servers/1/daily-restart", json={"time": "06:00"}, headers=_bearer(admin))

    first = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert first.json()["restart_at"] is not None

    cancel_r = await client.post("/api/plugin/cancel-restart", json={"server_num": 1}, headers=_hdr())
    assert cancel_r.status_code == 200
    assert cancel_r.json() == {"success": True}

    # daily_restart_time must still be set — cancel-restart never touches it.
    dr = await client.get("/api/admin/servers/1/daily-restart", headers=_bearer(admin))
    assert dr.json() == {"daily_restart_time": "06:00"}

    second = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert second.json()["restart_at"] is not None


async def test_clearing_daily_restart_time_stops_rearming(client, db_session):
    admin = await _make_admin(db_session)
    await _set_plugin_key(db_session)
    await client.post("/api/admin/servers/1/daily-restart", json={"time": "06:00"}, headers=_bearer(admin))

    # Arm it, then cancel the armed one-off restart — mirrors what the plugin does right
    # after it actually executes a restart. daily_restart_time is still set at this point.
    await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    await client.post("/api/plugin/cancel-restart", json={"server_num": 1}, headers=_hdr())

    del_r = await client.delete("/api/admin/servers/1/daily-restart", headers=_bearer(admin))
    assert del_r.status_code == 200
    assert del_r.json() == {"success": True}

    r = await client.get("/api/plugin/restart-status", params={"server_num": 1}, headers=_hdr())
    assert r.json() == {"restart_at": None}
