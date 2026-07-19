"""Regression tests for in-game moderation bans (.ban/.unban admin chat commands):
- POST /api/plugin/ban, POST /api/plugin/unban, GET /api/plugin/due-unbans,
  GET /api/plugin/ban-status (plugin-key gated)
- GET /api/admin/bans, POST /api/admin/bans/{id}/unban (admin-JWT gated)

See models.Ban's docstring for the full active/unban_at/unbanned_at lifecycle: a ban is
"active" for as long as unbanned_at is NULL, regardless of what unban_at currently says."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import Ban, Setting, User

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-bans"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def _make_admin(db_session, username="BansAdmin"):
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


def _iso_z(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat() + "Z"


# ─── POST /api/plugin/ban ───────────────────────────────────────────────────

async def test_plugin_ban_requires_plugin_key(client, db_session):
    r = await client.post(
        "/api/plugin/ban",
        json={"steam_id": "1", "character_name": "X", "server_num": 1, "admin_name": "Admin", "reason": "cheat", "unban_at": None},
    )
    assert r.status_code == 401


async def test_plugin_ban_permanent(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/ban",
        json={
            "steam_id": "76561198000000100",
            "character_name": "Vampire A",
            "server_num": 1,
            "admin_name": "Overseer",
            "reason": "duping",
            "unban_at": None,
        },
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"success": True}

    admin = await _make_admin(db_session)
    list_r = await client.get("/api/admin/bans", headers=_bearer(admin))
    bans = list_r.json()["bans"]
    assert len(bans) == 1
    b = bans[0]
    assert b["steam_id"] == "76561198000000100"
    assert b["character_name"] == "Vampire A"
    assert b["admin_name"] == "Overseer"
    assert b["reason"] == "duping"
    assert b["unban_at"] is None
    assert b["server_num"] == 1


async def test_plugin_ban_temp_with_future_timestamp(client, db_session):
    await _set_plugin_key(db_session)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await client.post(
        "/api/plugin/ban",
        json={
            "steam_id": "76561198000000101",
            "character_name": "Vampire B",
            "server_num": 1,
            "admin_name": "Overseer",
            "reason": "toxicity",
            "unban_at": _iso_z(future),
        },
        headers=_hdr(),
    )
    assert r.status_code == 200

    admin = await _make_admin(db_session)
    list_r = await client.get("/api/admin/bans", headers=_bearer(admin))
    bans = list_r.json()["bans"]
    assert len(bans) == 1
    assert bans[0]["unban_at"] is not None
    assert bans[0]["unban_at"].endswith("Z")


# ─── POST /api/plugin/unban ─────────────────────────────────────────────────

async def test_plugin_unban_marks_active_ban_resolved(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Ban(
        server_num=1, steam_id="steam-unban-1", character_name="C1",
        admin_name="Admin1", reason="r1", banned_at=datetime.utcnow(), unban_at=None,
    ))
    await db_session.commit()

    r = await client.post("/api/plugin/unban", json={"steam_id": "steam-unban-1", "server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"success": True}

    admin = await _make_admin(db_session)
    list_r = await client.get("/api/admin/bans", headers=_bearer(admin))
    assert list_r.json()["bans"] == []


async def test_plugin_unban_is_noop_when_nothing_active(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/unban", json={"steam_id": "no-such-steam-id", "server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"success": True}


async def test_plugin_unban_clears_ban_issued_on_different_server(client, db_session):
    """Cross-server: .unban run on server 2 must clear a ban that was originally
    issued on server 1 — see the cross-server enforcement change."""
    await _set_plugin_key(db_session)
    db_session.add(Ban(
        server_num=1, steam_id="steam-unban-cross", character_name="C1",
        admin_name="Admin1", reason="r1", banned_at=datetime.utcnow(), unban_at=None,
    ))
    await db_session.commit()

    r = await client.post("/api/plugin/unban", json={"steam_id": "steam-unban-cross", "server_num": 2}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"success": True}

    status_r = await client.get(
        "/api/plugin/ban-status", params={"steam_id": "steam-unban-cross", "server_num": 1}, headers=_hdr()
    )
    assert status_r.json() == {"banned": False}


# ─── GET /api/plugin/due-unbans ─────────────────────────────────────────────

async def test_due_unbans_returns_only_expired_bans_and_consumes_them(client, db_session):
    await _set_plugin_key(db_session)
    now = datetime.utcnow()
    db_session.add_all([
        Ban(server_num=1, steam_id="expired-1", character_name="Expired One",
            admin_name="A", reason="r", banned_at=now - timedelta(hours=2), unban_at=now - timedelta(minutes=5)),
        Ban(server_num=1, steam_id="future-1", character_name="Future One",
            admin_name="A", reason="r", banned_at=now - timedelta(hours=2), unban_at=now + timedelta(hours=1)),
        Ban(server_num=1, steam_id="permanent-1", character_name="Permanent One",
            admin_name="A", reason="r", banned_at=now - timedelta(hours=2), unban_at=None),
    ])
    await db_session.commit()

    r = await client.get("/api/plugin/due-unbans", params={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    unbans = r.json()["unbans"]
    assert len(unbans) == 1
    assert unbans[0] == {"steam_id": "expired-1", "character_name": "Expired One"}

    # Second call: the due row was consumed (unbanned_at set), so it's no longer due.
    r2 = await client.get("/api/plugin/due-unbans", params={"server_num": 1}, headers=_hdr())
    assert r2.json()["unbans"] == []


async def test_due_unbans_empty_array_never_errors(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/due-unbans", params={"server_num": 5}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"unbans": []}


async def test_due_unbans_scoped_per_server(client, db_session):
    await _set_plugin_key(db_session)
    now = datetime.utcnow()
    db_session.add_all([
        Ban(server_num=1, steam_id="s1-expired", character_name="S1",
            admin_name="A", reason="r", banned_at=now - timedelta(hours=2), unban_at=now - timedelta(minutes=1)),
        Ban(server_num=2, steam_id="s2-expired", character_name="S2",
            admin_name="A", reason="r", banned_at=now - timedelta(hours=2), unban_at=now - timedelta(minutes=1)),
    ])
    await db_session.commit()

    r1 = await client.get("/api/plugin/due-unbans", params={"server_num": 1}, headers=_hdr())
    assert [u["steam_id"] for u in r1.json()["unbans"]] == ["s1-expired"]

    r2 = await client.get("/api/plugin/due-unbans", params={"server_num": 2}, headers=_hdr())
    assert [u["steam_id"] for u in r2.json()["unbans"]] == ["s2-expired"]


# ─── GET /api/plugin/ban-status ─────────────────────────────────────────────

async def test_ban_status_false_for_unbanned_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get(
        "/api/plugin/ban-status", params={"steam_id": "no-such-steam-id", "server_num": 1}, headers=_hdr()
    )
    assert r.status_code == 200
    assert r.json() == {"banned": False}


async def test_ban_status_true_with_full_details_for_active_ban(client, db_session):
    await _set_plugin_key(db_session)
    future = datetime.utcnow() + timedelta(days=1)
    db_session.add(Ban(
        server_num=1, steam_id="status-active-1", character_name="Target",
        admin_name="Overseer", reason="cheating", banned_at=datetime.utcnow(), unban_at=future,
    ))
    await db_session.commit()

    r = await client.get(
        "/api/plugin/ban-status", params={"steam_id": "status-active-1", "server_num": 1}, headers=_hdr()
    )
    assert r.status_code == 200
    body = r.json()
    assert body["banned"] is True
    assert body["admin_name"] == "Overseer"
    assert body["reason"] == "cheating"
    assert body["unban_at"] is not None
    assert body["unban_at"].endswith("Z")


async def test_ban_status_false_for_already_unbanned(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Ban(
        server_num=1, steam_id="status-lifted-1", character_name="Target",
        admin_name="A", reason="r", banned_at=datetime.utcnow(), unban_at=None,
        unbanned_at=datetime.utcnow(),
    ))
    await db_session.commit()

    r = await client.get(
        "/api/plugin/ban-status", params={"steam_id": "status-lifted-1", "server_num": 1}, headers=_hdr()
    )
    assert r.status_code == 200
    assert r.json() == {"banned": False}


async def test_ban_status_cross_server(client, db_session):
    """A ban issued on one server blocks connecting to every tracked server — see
    the cross-server enforcement change in GET /api/plugin/ban-status. server_num
    is still accepted as a query param (the plugin always sends its own), but no
    longer used to filter the lookup."""
    await _set_plugin_key(db_session)
    db_session.add(Ban(
        server_num=2, steam_id="status-other-server", character_name="Target",
        admin_name="A", reason="r", banned_at=datetime.utcnow(), unban_at=None,
    ))
    await db_session.commit()

    r = await client.get(
        "/api/plugin/ban-status", params={"steam_id": "status-other-server", "server_num": 1}, headers=_hdr()
    )
    assert r.status_code == 200
    assert r.json()["banned"] is True
    assert r.json()["admin_name"] == "A"


# ─── GET /api/admin/bans ────────────────────────────────────────────────────

async def test_admin_bans_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/bans")
    assert r.status_code == 401


async def test_admin_bans_excludes_already_resolved(client, db_session):
    now = datetime.utcnow()
    db_session.add_all([
        Ban(server_num=1, steam_id="active-1", character_name="Active",
            admin_name="A", reason="r", banned_at=now, unban_at=None),
        Ban(server_num=1, steam_id="resolved-1", character_name="Resolved",
            admin_name="A", reason="r", banned_at=now, unban_at=None, unbanned_at=now),
    ])
    await db_session.commit()

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/bans", headers=_bearer(admin))
    assert r.status_code == 200
    steam_ids = [b["steam_id"] for b in r.json()["bans"]]
    assert steam_ids == ["active-1"]
    # Default (no status param) matches status=active explicitly.
    assert r.json() == (await client.get("/api/admin/bans", params={"status": "active"}, headers=_bearer(admin))).json()


async def test_admin_bans_status_resolved_returns_only_lifted(client, db_session):
    now = datetime.utcnow()
    db_session.add_all([
        Ban(server_num=1, steam_id="active-2", character_name="Active",
            admin_name="A", reason="r", banned_at=now, unban_at=None),
        Ban(server_num=1, steam_id="resolved-2", character_name="Resolved",
            admin_name="A", reason="r", banned_at=now, unban_at=None, unbanned_at=now),
    ])
    await db_session.commit()

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/bans", params={"status": "resolved"}, headers=_bearer(admin))
    assert r.status_code == 200
    bans = r.json()["bans"]
    assert [b["steam_id"] for b in bans] == ["resolved-2"]
    assert bans[0]["unbanned_at"] is not None
    assert bans[0]["unbanned_at"].endswith("Z")


async def test_admin_bans_status_all_returns_both(client, db_session):
    now = datetime.utcnow()
    db_session.add_all([
        Ban(server_num=1, steam_id="active-3", character_name="Active",
            admin_name="A", reason="r", banned_at=now, unban_at=None),
        Ban(server_num=1, steam_id="resolved-3", character_name="Resolved",
            admin_name="A", reason="r", banned_at=now, unban_at=None, unbanned_at=now),
    ])
    await db_session.commit()

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/bans", params={"status": "all"}, headers=_bearer(admin))
    assert r.status_code == 200
    steam_ids = {b["steam_id"] for b in r.json()["bans"]}
    assert steam_ids == {"active-3", "resolved-3"}


async def test_admin_bans_active_includes_null_unbanned_at_field(client, db_session):
    db_session.add(Ban(
        server_num=1, steam_id="active-4", character_name="Active",
        admin_name="A", reason="r", banned_at=datetime.utcnow(), unban_at=None,
    ))
    await db_session.commit()

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/bans", headers=_bearer(admin))
    assert r.json()["bans"][0]["unbanned_at"] is None


# ─── POST /api/admin/bans/{id}/unban ────────────────────────────────────────

async def test_admin_unban_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/bans/1/unban")
    assert r.status_code == 401


async def test_admin_unban_sets_unban_at_to_now(client, db_session):
    admin = await _make_admin(db_session)
    ban = Ban(
        server_num=1, steam_id="admin-unban-1", character_name="Target",
        admin_name="A", reason="r", banned_at=datetime.utcnow(),
        unban_at=datetime.utcnow() + timedelta(days=3),
    )
    db_session.add(ban)
    await db_session.commit()
    await db_session.refresh(ban)

    r = await client.post(f"/api/admin/bans/{ban.id}/unban", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json() == {"success": True}

    list_r = await client.get("/api/admin/bans", headers=_bearer(admin))
    updated = next(b for b in list_r.json()["bans"] if b["id"] == ban.id)
    unban_at = datetime.fromisoformat(updated["unban_at"][:-1]).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert abs((unban_at - now).total_seconds()) < 10


async def test_admin_unban_404_for_nonexistent_ban(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post("/api/admin/bans/999999/unban", headers=_bearer(admin))
    assert r.status_code == 404


async def test_admin_unban_404_for_already_resolved_ban(client, db_session):
    admin = await _make_admin(db_session)
    now = datetime.utcnow()
    ban = Ban(
        server_num=1, steam_id="already-resolved", character_name="Target",
        admin_name="A", reason="r", banned_at=now, unban_at=None, unbanned_at=now,
    )
    db_session.add(ban)
    await db_session.commit()
    await db_session.refresh(ban)

    r = await client.post(f"/api/admin/bans/{ban.id}/unban", headers=_bearer(admin))
    assert r.status_code == 404


async def test_admin_unban_makes_it_due_on_next_plugin_poll(client, db_session):
    """Clicking "Разбанить" on the site sets unban_at to now — the plugin's very next
    GET /api/plugin/due-unbans poll should pick it up and execute the real in-game unban."""
    await _set_plugin_key(db_session)
    admin = await _make_admin(db_session)
    ban = Ban(
        server_num=1, steam_id="force-unban-1", character_name="ForceUnbanned",
        admin_name="A", reason="r", banned_at=datetime.utcnow(),
        unban_at=datetime.utcnow() + timedelta(days=10),
    )
    db_session.add(ban)
    await db_session.commit()
    await db_session.refresh(ban)

    await client.post(f"/api/admin/bans/{ban.id}/unban", headers=_bearer(admin))

    r = await client.get("/api/plugin/due-unbans", params={"server_num": 1}, headers=_hdr())
    assert [u["steam_id"] for u in r.json()["unbans"]] == ["force-unban-1"]
