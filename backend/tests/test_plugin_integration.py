"""Regression tests for the BepInEx plugin integration endpoints (/api/plugin/*):
in-game .register/.login account linking, gated by the shared plugin_api_key secret."""
import pytest

from backend.auth import get_password_hash
from backend.models import Setting, User

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def test_register_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "111", "character_name": "NoKey", "password": "password1"},
    )
    assert r.status_code == 401


async def test_register_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "111", "character_name": "WrongKey", "password": "password1"},
        headers=_hdr("not-the-real-key"),
    )
    assert r.status_code == 401


async def test_plugin_key_unset_rejects_everything(client, db_session):
    # No plugin_api_key Setting row at all — must fail closed, not treat "" == "" as a match.
    r = await client.get("/api/plugin/status", params={"steam_id": "111"}, headers=_hdr(""))
    assert r.status_code == 401


async def test_register_creates_account_linked_by_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000001", "character_name": "Vampire Lord", "password": "password1", "server_num": 1},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["username"] == "Vampire Lord"

    status_r = await client.get("/api/plugin/status", params={"steam_id": "76561198000000001"}, headers=_hdr())
    assert status_r.json()["registered"] is True


async def test_register_rejects_duplicate_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    payload = {"steam_id": "76561198000000002", "character_name": "First Name", "password": "password1"}
    r1 = await client.post("/api/plugin/register", json=payload, headers=_hdr())
    assert r1.status_code == 200

    payload2 = {**payload, "character_name": "Second Name"}
    r2 = await client.post("/api/plugin/register", json=payload2, headers=_hdr())
    assert r2.status_code == 409
    assert r2.json()["detail"] == "already_registered"


async def test_register_rejects_username_taken_by_another_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    r1 = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000003", "character_name": "SameName", "password": "password1"},
        headers=_hdr(),
    )
    assert r1.status_code == 200

    r2 = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000004", "character_name": "SameName", "password": "password1"},
        headers=_hdr(),
    )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "username_taken"


async def test_register_rejects_invalid_character_name(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000005", "character_name": "x", "password": "password1"},  # too short
        headers=_hdr(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_username"


async def test_register_rejects_short_password(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000006", "character_name": "ShortPwd", "password": "123"},
        headers=_hdr(),
    )
    assert r.status_code == 422  # pydantic validation error


async def test_login_links_existing_web_account_by_username_and_password(client, db_session):
    await _set_plugin_key(db_session)
    # Simulates an account created via the website, never linked to a game account yet.
    web_user = User(
        username="WebRegistered",
        email="webregistered@example.com",
        hashed_password=get_password_hash("mypassword"),
        role="user",
    )
    db_session.add(web_user)
    await db_session.commit()
    await db_session.refresh(web_user)
    assert web_user.steam_id is None

    r = await client.post(
        "/api/plugin/login",
        json={"steam_id": "76561198000000007", "character_name": "WebRegistered", "password": "mypassword"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["success"] is True

    await db_session.refresh(web_user)
    assert web_user.steam_id == "76561198000000007"


async def test_login_rejects_wrong_password(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(User(username="PwdCheck", email="pwdcheck@example.com", hashed_password=get_password_hash("realpassword"), role="user"))
    await db_session.commit()

    r = await client.post(
        "/api/plugin/login",
        json={"steam_id": "76561198000000008", "character_name": "PwdCheck", "password": "wrongpassword"},
        headers=_hdr(),
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "bad_credentials"


async def test_login_rejects_unknown_username(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/login",
        json={"steam_id": "76561198000000009", "character_name": "NoSuchUser", "password": "whatever1"},
        headers=_hdr(),
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "bad_credentials"


async def test_login_rejects_account_already_linked_to_another_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(User(
        username="AlreadyLinked",
        email="alreadylinked@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id="76561198000000010",
    ))
    await db_session.commit()

    r = await client.post(
        "/api/plugin/login",
        json={"steam_id": "76561198000000099", "character_name": "AlreadyLinked", "password": "password1"},
        headers=_hdr(),
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "linked_elsewhere"


async def test_login_is_idempotent_for_the_same_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(User(
        username="SameSteamId",
        email="samesteamid@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id="76561198000000011",
    ))
    await db_session.commit()

    r = await client.post(
        "/api/plugin/login",
        json={"steam_id": "76561198000000011", "character_name": "SameSteamId", "password": "password1"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["success"] is True
