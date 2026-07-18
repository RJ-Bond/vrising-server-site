"""Regression tests for per-server plugin API keys (ServerApiKey table):
- GET/PUT /api/admin/server-api-key?server_num=N (admin-gated CRUD over the override)
- The reworked _require_plugin_key dependency in main.py, which now checks a per-server
  key first (ServerApiKey row for the request's server_num) and falls back to the
  global "plugin_api_key" Setting only when no per-server row exists for that server_num.

server_num extraction is covered for both flavors of plugin endpoint: a GET that takes
server_num as a query param (/api/plugin/announcements) and a POST that takes it as a
JSON body field (/api/plugin/heartbeat) — the dependency reads the body via
request.json() in the body case, relying on Starlette's request-body caching so the
endpoint's own Pydantic model can still parse it afterward; the heartbeat tests assert
on the persisted row, not just the HTTP status, to actually prove that still works.
"""
import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import PluginHeartbeat, ServerApiKey, Setting, User

pytestmark = pytest.mark.asyncio

GLOBAL_KEY = "global-plugin-key-123"
SERVER2_KEY = "server-2-only-key-456"


async def _set_global_key(db_session, value=GLOBAL_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


async def _set_server_key(db_session, server_num, value):
    db_session.add(ServerApiKey(server_num=server_num, api_key=value))
    await db_session.commit()


def _hdr(key):
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


# ─── Fallback behavior (no per-server key configured) ──────────────────────────

async def test_server_without_own_key_still_works_via_global_key(client, db_session):
    """Backward compat: a server that never got its own ServerApiKey row keeps
    authenticating with the existing shared global key."""
    await _set_global_key(db_session)
    r = await client.get(
        "/api/plugin/announcements", params={"server_num": 1}, headers=_hdr(GLOBAL_KEY)
    )
    assert r.status_code == 200


async def test_server_without_own_key_rejects_wrong_key(client, db_session):
    await _set_global_key(db_session)
    r = await client.get(
        "/api/plugin/announcements", params={"server_num": 1}, headers=_hdr("not-the-key")
    )
    assert r.status_code == 401


# ─── Per-server key takes priority, no fallback once configured ────────────────

async def test_server_with_own_key_rejects_the_global_key(client, db_session):
    await _set_global_key(db_session)
    await _set_server_key(db_session, 2, SERVER2_KEY)
    r = await client.get(
        "/api/plugin/announcements", params={"server_num": 2}, headers=_hdr(GLOBAL_KEY)
    )
    assert r.status_code == 401


async def test_server_with_own_key_accepts_only_its_own_key(client, db_session):
    await _set_global_key(db_session)
    await _set_server_key(db_session, 2, SERVER2_KEY)
    r = await client.get(
        "/api/plugin/announcements", params={"server_num": 2}, headers=_hdr(SERVER2_KEY)
    )
    assert r.status_code == 200


async def test_key_belonging_to_a_different_server_is_rejected(client, db_session):
    """A valid key for server 2 must not grant access when calling as server 3, even
    though server 3 has no key of its own (it should fall back to the global key, not
    accept server 2's key)."""
    await _set_global_key(db_session)
    await _set_server_key(db_session, 2, SERVER2_KEY)
    r = await client.get(
        "/api/plugin/announcements", params={"server_num": 3}, headers=_hdr(SERVER2_KEY)
    )
    assert r.status_code == 401


# ─── server_num extraction: query param (GET) vs JSON body (POST) ──────────────

async def test_server_num_extracted_from_query_param_on_get_endpoint(client, db_session):
    await _set_global_key(db_session)
    await _set_server_key(db_session, 2, SERVER2_KEY)
    # server_num=2 via query param must route to server 2's key, not the global one.
    ok = await client.get(
        "/api/plugin/announcements", params={"server_num": 2}, headers=_hdr(SERVER2_KEY)
    )
    assert ok.status_code == 200
    rejected = await client.get(
        "/api/plugin/announcements", params={"server_num": 2}, headers=_hdr(GLOBAL_KEY)
    )
    assert rejected.status_code == 401


async def test_server_num_extracted_from_json_body_on_post_endpoint(client, db_session):
    """/api/plugin/heartbeat has no query param for server_num — it's a body field —
    so this proves the dependency's request.json() fallback path works, AND that the
    endpoint's own Pydantic model can still read the body afterward (asserted via the
    persisted PluginHeartbeat row, not just the HTTP status)."""
    await _set_global_key(db_session)
    await _set_server_key(db_session, 2, SERVER2_KEY)

    rejected = await client.post(
        "/api/plugin/heartbeat",
        json={"server_num": 2, "server_name": "Wrong Key Server", "player_count": 1},
        headers=_hdr(GLOBAL_KEY),
    )
    assert rejected.status_code == 401

    ok = await client.post(
        "/api/plugin/heartbeat",
        json={"server_num": 2, "server_name": "Server Two", "player_count": 5},
        headers=_hdr(SERVER2_KEY),
    )
    assert ok.status_code == 200
    assert ok.json()["success"] is True

    # Prove the endpoint's own body parsing (PluginHeartbeatIn) still worked after the
    # dependency consumed request.json() for server_num extraction.
    result = await db_session.execute(
        select(PluginHeartbeat).where(PluginHeartbeat.server_num == 2)
    )
    hb = result.scalar_one_or_none()
    assert hb is not None
    assert hb.server_name == "Server Two"
    assert hb.player_count == 5


async def test_server_num_defaults_to_1_when_undeterminable(client, db_session):
    """No query param, no body (a GET with nothing) — must default to server_num=1,
    not error out, and use server 1's key/fallback."""
    await _set_global_key(db_session)
    r = await client.get("/api/plugin/status", params={"steam_id": "111"}, headers=_hdr(GLOBAL_KEY))
    assert r.status_code == 200


# ─── Admin GET/PUT /api/admin/server-api-key ────────────────────────────────────

async def test_admin_get_server_api_key_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/server-api-key?server_num=1")
    assert r.status_code == 401


async def test_admin_put_server_api_key_requires_admin_auth(client, db_session):
    r = await client.put("/api/admin/server-api-key?server_num=1", json={"api_key": "x"})
    assert r.status_code == 401


async def test_admin_get_server_api_key_returns_empty_when_unset(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/server-api-key?server_num=1", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json() == {"api_key": ""}


async def test_admin_put_then_get_round_trip(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    put_r = await client.put(
        "/api/admin/server-api-key?server_num=2", json={"api_key": "my-server-2-secret"}, headers=headers
    )
    assert put_r.status_code == 200
    assert put_r.json() == {"api_key": "my-server-2-secret"}

    get_r = await client.get("/api/admin/server-api-key?server_num=2", headers=headers)
    assert get_r.status_code == 200
    assert get_r.json() == {"api_key": "my-server-2-secret"}

    # Other servers unaffected.
    other_r = await client.get("/api/admin/server-api-key?server_num=1", headers=headers)
    assert other_r.json() == {"api_key": ""}


async def test_admin_put_empty_api_key_clears_the_override(client, db_session):
    """Saving an empty string is the 'clear my override' UX — it must delete the row
    (reverting to the global fallback), not persist an empty-string secret."""
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    await client.put("/api/admin/server-api-key?server_num=2", json={"api_key": "temp-secret"}, headers=headers)
    clear_r = await client.put("/api/admin/server-api-key?server_num=2", json={"api_key": ""}, headers=headers)
    assert clear_r.status_code == 200
    assert clear_r.json() == {"api_key": ""}

    result = await db_session.execute(select(ServerApiKey).where(ServerApiKey.server_num == 2))
    assert result.scalar_one_or_none() is None

    # And the server now actually falls back to the global key again.
    await _set_global_key(db_session)
    r = await client.get(
        "/api/plugin/announcements", params={"server_num": 2}, headers=_hdr(GLOBAL_KEY)
    )
    assert r.status_code == 200
