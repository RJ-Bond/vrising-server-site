"""Regression tests for GET /api/plugin/senior-admins — the site's admin/superadmin role
users, synced into js-plugin's local SeniorAdminSteamIds list. Same auth pattern as
test_plugin_warnings.py."""
import pytest

from backend.models import Setting, User
from backend.auth import get_password_hash

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def _make_user(db_session, username, role, steam_id):
    user = User(
        username=username,
        email=f"{username}@test.local",
        hashed_password=get_password_hash("pw"),
        role=role,
        steam_id=steam_id,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def test_returns_admin_and_superadmin_steam_ids(client, db_session):
    await _set_plugin_key(db_session)
    await _make_user(db_session, "TheAdmin", "admin", "76561198000000401")
    await _make_user(db_session, "TheSuperadmin", "superadmin", "76561198000000402")

    r = await client.get("/api/plugin/senior-admins", headers=_hdr())
    assert r.status_code == 200
    steam_ids = set(r.json()["steam_ids"])
    assert steam_ids == {"76561198000000401", "76561198000000402"}


async def test_excludes_moderator_and_plain_user_roles(client, db_session):
    await _set_plugin_key(db_session)
    await _make_user(db_session, "TheMod", "moderator", "76561198000000403")
    await _make_user(db_session, "PlainUser", "user", "76561198000000404")
    await _make_user(db_session, "TheAdmin", "admin", "76561198000000405")

    r = await client.get("/api/plugin/senior-admins", headers=_hdr())
    assert r.status_code == 200
    steam_ids = set(r.json()["steam_ids"])
    assert steam_ids == {"76561198000000405"}


async def test_excludes_admins_without_a_linked_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    await _make_user(db_session, "UnlinkedAdmin", "admin", None)

    r = await client.get("/api/plugin/senior-admins", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["steam_ids"] == []


async def test_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/senior-admins")
    assert r.status_code == 401


async def test_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/senior-admins", headers=_hdr("not-the-real-key"))
    assert r.status_code == 401
