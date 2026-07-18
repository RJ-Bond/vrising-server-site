"""Regression tests for the unified moderation log:
- POST /api/plugin/log-action (plugin-key gated)
- GET /api/admin/moderation-log (admin-JWT gated) — merges Ban/Warning/ModerationLogEntry
  rows into one chronological feed.

See models.ModerationLogEntry's docstring: it only stores the action types NOT already
captured by Ban (ban/unban) or Warning (warn)."""
from datetime import datetime, timedelta

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import Ban, Setting, User, Warning

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-modlog"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def _make_admin(db_session, username="ModLogAdmin"):
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


# ─── POST /api/plugin/log-action ─────────────────────────────────────────────

async def test_log_action_requires_plugin_key(client, db_session):
    r = await client.post("/api/plugin/log-action", json={"server_num": 1, "action": "kick"})
    assert r.status_code == 401


async def test_log_action_rejects_invalid_action(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/log-action",
        json={"server_num": 1, "action": "ban"},  # not allowed here — has its own endpoint
        headers=_hdr(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_action"


@pytest.mark.parametrize("action", ["kick", "mute", "unmute", "restart_scheduled", "restart_executed"])
async def test_log_action_accepts_all_five_valid_actions(client, db_session, action):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/log-action",
        json={
            "server_num": 1,
            "action": action,
            "admin_name": "Overseer",
            "target_name": "Target",
            "target_steam_id": "steam-x",
            "details": "some detail",
        },
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"success": True}


async def test_log_action_allows_null_admin_name_for_system_actions(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/log-action",
        json={"server_num": 1, "action": "restart_executed", "admin_name": None, "target_name": None, "target_steam_id": None, "details": "auto"},
        headers=_hdr(),
    )
    assert r.status_code == 200


# ─── GET /api/admin/moderation-log ───────────────────────────────────────────

async def test_moderation_log_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/moderation-log")
    assert r.status_code == 401


async def test_moderation_log_merges_all_three_sources_sorted_desc(client, db_session):
    await _set_plugin_key(db_session)
    now = datetime.utcnow()

    # Oldest: a ban
    db_session.add(Ban(
        server_num=1, steam_id="steam-1", character_name="P1",
        admin_name="AdminA", reason="cheating", banned_at=now - timedelta(minutes=30),
        unban_at=None,
    ))
    # Middle: a warning
    db_session.add(Warning(
        server_num=1, steam_id="steam-2", character_name="P2",
        reason="spam", admin_name="AdminB", created_at=now - timedelta(minutes=20),
    ))
    await db_session.commit()

    # Newest: a plugin-logged kick
    await client.post(
        "/api/plugin/log-action",
        json={"server_num": 1, "action": "kick", "admin_name": "AdminC", "target_name": "P3", "target_steam_id": "steam-3", "details": "afk"},
        headers=_hdr(),
    )

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/moderation-log", headers=_bearer(admin))
    assert r.status_code == 200
    log = r.json()["log"]
    actions = [e["action"] for e in log]
    assert actions == ["kick", "warn", "ban"]

    kick_entry = log[0]
    assert kick_entry["target_steam_id"] == "steam-3"
    assert kick_entry["admin_name"] == "AdminC"
    assert kick_entry["details"] == "afk"

    warn_entry = log[1]
    assert warn_entry["target_steam_id"] == "steam-2"
    assert warn_entry["details"] == "spam"

    ban_entry = log[2]
    assert ban_entry["target_steam_id"] == "steam-1"
    assert ban_entry["details"] == "cheating"


async def test_moderation_log_emits_unban_entry_for_lifted_bans(client, db_session):
    now = datetime.utcnow()
    db_session.add(Ban(
        server_num=1, steam_id="steam-lifted", character_name="Lifted",
        admin_name="AdminA", reason="r", banned_at=now - timedelta(hours=1),
        unban_at=now - timedelta(minutes=30), unbanned_at=now - timedelta(minutes=30),
    ))
    await db_session.commit()

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/moderation-log", headers=_bearer(admin))
    log = r.json()["log"]
    actions = [e["action"] for e in log]
    assert actions == ["unban", "ban"]  # unban (more recent timestamp) sorts first


async def test_moderation_log_respects_limit(client, db_session):
    now = datetime.utcnow()
    for i in range(5):
        db_session.add(Warning(
            server_num=1, steam_id=f"steam-{i}", character_name=f"P{i}",
            reason="r", admin_name="A", created_at=now - timedelta(minutes=i),
        ))
    await db_session.commit()

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/moderation-log", params={"limit": 2}, headers=_bearer(admin))
    assert len(r.json()["log"]) == 2


async def test_moderation_log_server_num_filter(client, db_session):
    now = datetime.utcnow()
    db_session.add_all([
        Warning(server_num=1, steam_id="s1", character_name="P1", reason="r", admin_name="A", created_at=now),
        Warning(server_num=2, steam_id="s2", character_name="P2", reason="r", admin_name="A", created_at=now),
    ])
    await db_session.commit()

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/moderation-log", params={"server_num": 2}, headers=_bearer(admin))
    log = r.json()["log"]
    assert len(log) == 1
    assert log[0]["target_steam_id"] == "s2"
