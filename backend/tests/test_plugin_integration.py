"""Regression tests for the BepInEx plugin integration endpoints (/api/plugin/*):
in-game .register/.login account linking, gated by the shared plugin_api_key secret."""
import pytest
from sqlalchemy import select

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


async def test_status_for_unlinked_steam_id_reports_not_registered_with_null_username(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/status", params={"steam_id": "76561198999999999"}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is False
    assert body["username"] is None


async def test_status_for_linked_steam_id_reports_registered_with_username(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(User(
        username="StatusCheckUser",
        email="statuscheckuser@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id="76561198000000012",
    ))
    await db_session.commit()

    r = await client.get("/api/plugin/status", params={"steam_id": "76561198000000012"}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is True
    assert body["username"] == "StatusCheckUser"


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
        json={"steam_id": "76561198000000006", "character_name": "ShortPwd", "password": "abc123"},  # < 8 chars
        headers=_hdr(),
    )
    assert r.status_code == 422  # pydantic validation error


async def test_register_rejects_letters_only_password(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000012", "character_name": "LettersOnly", "password": "onlyletters"},
        headers=_hdr(),
    )
    assert r.status_code == 422  # pydantic validation error


async def test_register_rejects_digits_only_password(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000013", "character_name": "DigitsOnly", "password": "12345678"},
        headers=_hdr(),
    )
    assert r.status_code == 422  # pydantic validation error


async def test_register_accepts_complex_password(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/register",
        json={"steam_id": "76561198000000014", "character_name": "ComplexPwd", "password": "correctH0rse"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


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


async def test_status_for_unlinked_steam_id_reports_null_rules_accepted(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/status", params={"steam_id": "76561198999999998"}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is False
    assert body["rules_accepted"] is None


async def test_status_for_registered_user_who_has_not_accepted_rules(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(User(
        username="RulesNotYet",
        email="rulesnotyet@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id="76561198000000020",
    ))
    await db_session.commit()

    r = await client.get("/api/plugin/status", params={"steam_id": "76561198000000020"}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is True
    assert body["rules_accepted"] is False


async def test_status_for_registered_user_who_accepted_rules(client, db_session):
    await _set_plugin_key(db_session)
    from datetime import datetime, timezone
    db_session.add(User(
        username="RulesAccepted",
        email="rulesaccepted@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id="76561198000000021",
        rules_accepted_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    r = await client.get("/api/plugin/status", params={"steam_id": "76561198000000021"}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is True
    assert body["rules_accepted"] is True


async def test_get_rules_returns_parsed_rules_array(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(
        key="rules",
        value='[{"icon":"🤝","text":"Be nice"},{"icon":"🚫","text":"No cheating"}]',
    ))
    await db_session.commit()

    r = await client.get("/api/plugin/rules", params={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["rules"] == [
        {"icon": "🤝", "text": "Be nice"},
        {"icon": "🚫", "text": "No cheating"},
    ]


async def test_get_rules_returns_empty_list_when_setting_missing(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/rules", params={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"rules": []}


async def test_get_rules_requires_plugin_key(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/rules", params={"server_num": 1})
    assert r.status_code == 401


async def test_accept_rules_sets_timestamp_for_registered_user(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(User(
        username="AcceptRulesUser",
        email="acceptrulesuser@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id="76561198000000022",
    ))
    await db_session.commit()

    r = await client.post(
        "/api/plugin/accept-rules",
        json={"steam_id": "76561198000000022", "server_num": 1},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"success": True}

    status_r = await client.get("/api/plugin/status", params={"steam_id": "76561198000000022"}, headers=_hdr())
    assert status_r.json()["rules_accepted"] is True


async def test_accept_rules_for_unknown_steam_id_returns_404(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/accept-rules",
        json={"steam_id": "76561198000000023", "server_num": 1},
        headers=_hdr(),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "not_registered"


async def test_accept_rules_twice_does_not_move_timestamp_forward(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(User(
        username="DoubleAccept",
        email="doubleaccept@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id="76561198000000024",
    ))
    await db_session.commit()

    r1 = await client.post(
        "/api/plugin/accept-rules",
        json={"steam_id": "76561198000000024", "server_num": 1},
        headers=_hdr(),
    )
    assert r1.status_code == 200
    assert r1.json()["success"] is True

    result = await db_session.execute(
        select(User).where(User.steam_id == "76561198000000024")
    )
    user = result.scalar_one()
    await db_session.refresh(user)
    first_accepted_at = user.rules_accepted_at
    assert first_accepted_at is not None

    r2 = await client.post(
        "/api/plugin/accept-rules",
        json={"steam_id": "76561198000000024", "server_num": 1},
        headers=_hdr(),
    )
    assert r2.status_code == 200
    assert r2.json()["success"] is True

    result2 = await db_session.execute(
        select(User).where(User.steam_id == "76561198000000024")
    )
    user2 = result2.scalar_one()
    await db_session.refresh(user2)
    assert user2.rules_accepted_at is not None
    assert user2.rules_accepted_at == first_accepted_at
