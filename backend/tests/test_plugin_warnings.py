"""Regression tests for the in-game moderation warnings log (/api/plugin/warn and
/api/plugin/warnings), gated by the shared plugin_api_key secret — same pattern as
test_plugin_integration.py."""
import pytest

from backend.models import Setting

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def test_warn_for_new_steam_id_returns_warning_count_one(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/warn",
        json={
            "steam_id": "76561198000000101",
            "character_name": "Rulebreaker",
            "reason": "Griefing at spawn",
            "admin_name": "AdminOne",
        },
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["warning_count"] == 1


async def test_second_warn_for_same_steam_id_increments_count(client, db_session):
    await _set_plugin_key(db_session)
    payload = {
        "steam_id": "76561198000000102",
        "character_name": "Repeat Offender",
        "reason": "Spamming chat",
        "admin_name": "AdminOne",
    }
    r1 = await client.post("/api/plugin/warn", json=payload, headers=_hdr())
    assert r1.status_code == 200
    assert r1.json()["warning_count"] == 1

    r2 = await client.post(
        "/api/plugin/warn",
        json={**payload, "reason": "Griefing a base"},
        headers=_hdr(),
    )
    assert r2.status_code == 200
    assert r2.json()["warning_count"] == 2


async def test_warnings_list_returns_both_most_recent_first(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76561198000000103"
    r1 = await client.post(
        "/api/plugin/warn",
        json={
            "steam_id": steam_id,
            "character_name": "Multi Warned",
            "reason": "First offense",
            "admin_name": "AdminA",
        },
        headers=_hdr(),
    )
    assert r1.status_code == 200
    r2 = await client.post(
        "/api/plugin/warn",
        json={
            "steam_id": steam_id,
            "character_name": "Multi Warned",
            "reason": "Second offense",
            "admin_name": "AdminB",
        },
        headers=_hdr(),
    )
    assert r2.status_code == 200

    list_r = await client.get(
        "/api/plugin/warnings", params={"steam_id": steam_id}, headers=_hdr()
    )
    assert list_r.status_code == 200
    warnings = list_r.json()["warnings"]
    assert len(warnings) == 2
    # Most recent first.
    assert warnings[0]["reason"] == "Second offense"
    assert warnings[0]["admin_name"] == "AdminB"
    assert warnings[1]["reason"] == "First offense"
    assert warnings[1]["admin_name"] == "AdminA"
    for w in warnings:
        assert w["created_at"].endswith("Z")
        assert w["server_num"] == 1


async def test_warnings_for_steam_id_with_none_returns_empty_list(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get(
        "/api/plugin/warnings",
        params={"steam_id": "76561198000000999"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"warnings": []}


async def test_warnings_do_not_leak_between_steam_ids(client, db_session):
    await _set_plugin_key(db_session)
    steam_id_a = "76561198000000104"
    steam_id_b = "76561198000000105"

    r_a = await client.post(
        "/api/plugin/warn",
        json={
            "steam_id": steam_id_a,
            "character_name": "PlayerA",
            "reason": "Reason A",
            "admin_name": "AdminA",
        },
        headers=_hdr(),
    )
    assert r_a.status_code == 200
    assert r_a.json()["warning_count"] == 1

    list_b = await client.get(
        "/api/plugin/warnings", params={"steam_id": steam_id_b}, headers=_hdr()
    )
    assert list_b.status_code == 200
    assert list_b.json() == {"warnings": []}

    list_a = await client.get(
        "/api/plugin/warnings", params={"steam_id": steam_id_a}, headers=_hdr()
    )
    assert list_a.status_code == 200
    assert len(list_a.json()["warnings"]) == 1


async def test_warn_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/warn",
        json={
            "steam_id": "76561198000000201",
            "character_name": "NoKey",
            "reason": "Testing auth",
            "admin_name": "AdminOne",
        },
    )
    assert r.status_code == 401


async def test_warn_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/warn",
        json={
            "steam_id": "76561198000000202",
            "character_name": "WrongKey",
            "reason": "Testing auth",
            "admin_name": "AdminOne",
        },
        headers=_hdr("not-the-real-key"),
    )
    assert r.status_code == 401


async def test_warnings_list_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get(
        "/api/plugin/warnings", params={"steam_id": "76561198000000203"}
    )
    assert r.status_code == 401


async def test_warnings_list_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get(
        "/api/plugin/warnings",
        params={"steam_id": "76561198000000204"},
        headers=_hdr("not-the-real-key"),
    )
    assert r.status_code == 401
