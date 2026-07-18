"""Regression tests for per-server connect/disconnect message templates:
- GET/PUT /api/admin/message-templates?server_num=N (admin-gated, ServerMessageTemplate table)
- GET /api/plugin/message-templates?server_num=N (plugin-facing, reads the same table)

Replaces the old global "connect_message_template"/"disconnect_message_template" Settings
now that the plugin can run on more than one server."""
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


# ─── Plugin-facing polling endpoint ────────────────────────────────────────

async def test_plugin_message_templates_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/message-templates")
    assert r.status_code == 401


async def test_plugin_message_templates_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/message-templates", headers=_hdr("not-the-real-key"))
    assert r.status_code == 401


async def test_plugin_message_templates_returns_empty_strings_when_unset(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/message-templates", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"connect": "", "disconnect": ""}


async def test_plugin_message_templates_defaults_server_num_to_1(client, db_session):
    """An old plugin build that never sends server_num should still get server 1's templates."""
    await _set_plugin_key(db_session)
    admin = await _make_admin(db_session)
    put_r = await client.put(
        "/api/admin/message-templates",
        json={"connect": "<color=#00FF00>{name} в сети</color>"},
        headers=_bearer(admin),
    )
    assert put_r.status_code == 200

    r = await client.get("/api/plugin/message-templates", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"connect": "<color=#00FF00>{name} в сети</color>", "disconnect": ""}


# ─── Admin GET/PUT ──────────────────────────────────────────────────────────

async def test_admin_get_message_templates_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/message-templates")
    assert r.status_code == 401


async def test_admin_put_message_templates_requires_admin_auth(client, db_session):
    r = await client.put("/api/admin/message-templates", json={"connect": "hi"})
    assert r.status_code == 401


async def test_admin_get_message_templates_returns_empty_when_unset(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/message-templates?server_num=1", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json() == {"connect": "", "disconnect": ""}


async def test_admin_put_then_get_round_trip(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    put_r = await client.put(
        "/api/admin/message-templates?server_num=1",
        json={
            "connect": "<color=#00FF00>{name} присоединился</color>",
            "disconnect": "<color=#FF3355>{name} покинул сервер</color>",
        },
        headers=headers,
    )
    assert put_r.status_code == 200
    assert put_r.json() == {
        "connect": "<color=#00FF00>{name} присоединился</color>",
        "disconnect": "<color=#FF3355>{name} покинул сервер</color>",
    }

    get_r = await client.get("/api/admin/message-templates?server_num=1", headers=headers)
    assert get_r.status_code == 200
    assert get_r.json() == {
        "connect": "<color=#00FF00>{name} присоединился</color>",
        "disconnect": "<color=#FF3355>{name} покинул сервер</color>",
    }


async def test_admin_put_partial_update_leaves_other_field_untouched(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    await client.put(
        "/api/admin/message-templates?server_num=1",
        json={"connect": "<color=#00FF00>в сети</color>", "disconnect": "<color=#FF3355>вышел</color>"},
        headers=headers,
    )
    # Partial update: only touch "connect", "disconnect" must survive untouched.
    r = await client.put(
        "/api/admin/message-templates?server_num=1",
        json={"connect": "<color=#00FF00>обновлено</color>"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json() == {
        "connect": "<color=#00FF00>обновлено</color>",
        "disconnect": "<color=#FF3355>вышел</color>",
    }


async def test_admin_message_templates_are_scoped_per_server(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    await client.put(
        "/api/admin/message-templates?server_num=1",
        json={"connect": "server1 connect"},
        headers=headers,
    )
    await client.put(
        "/api/admin/message-templates?server_num=2",
        json={"connect": "server2 connect"},
        headers=headers,
    )

    r1 = await client.get("/api/admin/message-templates?server_num=1", headers=headers)
    assert r1.json()["connect"] == "server1 connect"

    r2 = await client.get("/api/admin/message-templates?server_num=2", headers=headers)
    assert r2.json()["connect"] == "server2 connect"


async def test_plugin_message_templates_are_scoped_per_server(client, db_session):
    admin = await _make_admin(db_session)
    await _set_plugin_key(db_session)

    await client.put(
        "/api/admin/message-templates?server_num=1",
        json={"connect": "server1 connect", "disconnect": "server1 disconnect"},
        headers=_bearer(admin),
    )
    await client.put(
        "/api/admin/message-templates?server_num=2",
        json={"connect": "server2 connect", "disconnect": "server2 disconnect"},
        headers=_bearer(admin),
    )

    r1 = await client.get("/api/plugin/message-templates?server_num=1", headers=_hdr())
    assert r1.json() == {"connect": "server1 connect", "disconnect": "server1 disconnect"}

    r2 = await client.get("/api/plugin/message-templates?server_num=2", headers=_hdr())
    assert r2.json() == {"connect": "server2 connect", "disconnect": "server2 disconnect"}
