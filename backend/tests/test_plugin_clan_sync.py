"""Regression tests for the game-synced clan system:
POST /api/plugin/clans/sync (gated by the shared plugin_api_key secret, pushes the
plugin's FULL current clan roster for one server_num) and the read-only public
GET /api/clans / GET /api/clans/{id} endpoints that read back the synced data."""
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


def _sync_body(server_num=1):
    return {
        "server_num": server_num,
        "clans": [
            {
                "clan_guid": "guid-alpha",
                "name": "Alpha Clan",
                "motto": "First!",
                "members": [
                    {"steam_id": "111", "character_name": "AlphaLeader", "role": "leader"},
                    {"steam_id": "222", "character_name": "AlphaMember", "role": "member"},
                ],
            },
        ],
    }


async def test_sync_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/clans/sync", json=_sync_body())
    assert r.status_code == 401


async def test_sync_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/clans/sync", json=_sync_body(), headers=_hdr("not-the-real-key"))
    assert r.status_code == 401


async def test_sync_creates_clans_and_members(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/clans/sync", json=_sync_body(), headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["clan_count"] == 1

    list_r = await client.get("/api/clans")
    assert list_r.status_code == 200
    clans = list_r.json()
    assert len(clans) == 1
    assert clans[0]["name"] == "Alpha Clan"
    assert clans[0]["member_count"] == 2


async def test_second_sync_fully_replaces_prior_set(client, db_session):
    await _set_plugin_key(db_session)
    await client.post("/api/plugin/clans/sync", json=_sync_body(), headers=_hdr())

    replacement = {
        "server_num": 1,
        "clans": [
            {
                "clan_guid": "guid-beta",
                "name": "Beta Clan",
                "motto": "Second!",
                "members": [
                    {"steam_id": "333", "character_name": "BetaLeader", "role": "leader"},
                ],
            },
        ],
    }
    r = await client.post("/api/plugin/clans/sync", json=replacement, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["clan_count"] == 1

    list_r = await client.get("/api/clans")
    assert list_r.status_code == 200
    clans = list_r.json()
    assert len(clans) == 1
    assert clans[0]["name"] == "Beta Clan"
    names = [c["name"] for c in clans]
    assert "Alpha Clan" not in names


async def test_sync_for_different_server_num_is_independent(client, db_session):
    await _set_plugin_key(db_session)
    await client.post("/api/plugin/clans/sync", json=_sync_body(server_num=1), headers=_hdr())
    await client.post("/api/plugin/clans/sync", json=_sync_body(server_num=2), headers=_hdr())

    list_r = await client.get("/api/clans")
    assert list_r.status_code == 200
    clans = list_r.json()
    # Same clan_guid synced independently for both servers — both rows should exist.
    assert len(clans) == 2
    assert {c["server_num"] for c in clans} == {1, 2}


async def test_clan_list_search_filters_by_name(client, db_session):
    await _set_plugin_key(db_session)
    body = {
        "server_num": 1,
        "clans": [
            {"clan_guid": "guid-a", "name": "Blood Fangs", "motto": "", "members": []},
            {"clan_guid": "guid-b", "name": "Night Watch", "motto": "", "members": []},
        ],
    }
    await client.post("/api/plugin/clans/sync", json=body, headers=_hdr())

    r = await client.get("/api/clans", params={"search": "blood", "limit": 5})
    assert r.status_code == 200
    names = [c["name"] for c in r.json()]
    assert names == ["Blood Fangs"]


async def test_clan_detail_enriches_members_with_linked_site_accounts(client, db_session):
    await _set_plugin_key(db_session)

    linked_user = User(
        username="LinkedPlayer",
        email="linked@example.com",
        hashed_password=get_password_hash("x"),
        steam_id="111",
        avatar_url="https://example.com/avatar.png",
    )
    db_session.add(linked_user)
    await db_session.commit()

    await client.post("/api/plugin/clans/sync", json=_sync_body(), headers=_hdr())

    list_r = await client.get("/api/clans")
    clan_id = list_r.json()[0]["id"]

    detail_r = await client.get(f"/api/clans/{clan_id}")
    assert detail_r.status_code == 200
    detail = detail_r.json()
    assert detail["name"] == "Alpha Clan"
    members = {m["steam_id"]: m for m in detail["members"]}
    assert len(members) == 2

    linked = members["111"]
    assert linked["character_name"] == "AlphaLeader"
    assert linked["role"] == "leader"
    assert linked["username"] == "LinkedPlayer"
    assert linked["avatar_url"] == "https://example.com/avatar.png"

    unlinked = members["222"]
    assert unlinked["character_name"] == "AlphaMember"
    assert unlinked["username"] is None
    assert unlinked["avatar_url"] is None


async def test_clan_detail_404_for_missing_clan(client, db_session):
    r = await client.get("/api/clans/999999")
    assert r.status_code == 404


async def test_repeated_sync_with_same_clan_and_member_does_not_accumulate(client, db_session):
    """Regression test for a bug where GameClanMember rows were never actually deleted
    (the DB-level ON DELETE CASCADE the ORM annotation implies is not enforced by
    SQLite without PRAGMA foreign_keys = ON, which this app never sets), so every sync
    cycle for the same server_num silently piled another duplicate member row onto the
    same clan_id — since SQLite's plain INTEGER PRIMARY KEY reuses the freed rowid once
    the game_clans table is emptied by delete(GameClan), the "new" clan kept landing on
    the same id as before, so old orphaned members kept counting toward it forever."""
    await _set_plugin_key(db_session)

    body = {
        "server_num": 1,
        "clans": [
            {
                "clan_guid": "guid-solo",
                "name": "Solo Clan",
                "motto": "",
                "members": [
                    {"steam_id": "111", "character_name": "SoloLeader", "role": "leader"},
                ],
            },
        ],
    }

    for _ in range(2):
        r = await client.post("/api/plugin/clans/sync", json=body, headers=_hdr())
        assert r.status_code == 200

    list_r = await client.get("/api/clans")
    assert list_r.status_code == 200
    clans = list_r.json()
    assert len(clans) == 1
    assert clans[0]["name"] == "Solo Clan"
    assert clans[0]["member_count"] == 1

    detail_r = await client.get(f"/api/clans/{clans[0]['id']}")
    assert detail_r.status_code == 200
    assert len(detail_r.json()["members"]) == 1


async def test_repeated_sync_with_different_member_replaces_not_adds(client, db_session):
    """Same accumulation bug, but the second sync reports a different steam_id for the
    same clan_guid (e.g. the old member left and a new one joined) — the old member must
    be dropped, not kept alongside the new one."""
    await _set_plugin_key(db_session)

    first = {
        "server_num": 1,
        "clans": [
            {
                "clan_guid": "guid-solo",
                "name": "Solo Clan",
                "motto": "",
                "members": [
                    {"steam_id": "111", "character_name": "OldMember", "role": "leader"},
                ],
            },
        ],
    }
    second = {
        "server_num": 1,
        "clans": [
            {
                "clan_guid": "guid-solo",
                "name": "Solo Clan",
                "motto": "",
                "members": [
                    {"steam_id": "999", "character_name": "NewMember", "role": "leader"},
                ],
            },
        ],
    }

    r1 = await client.post("/api/plugin/clans/sync", json=first, headers=_hdr())
    assert r1.status_code == 200
    r2 = await client.post("/api/plugin/clans/sync", json=second, headers=_hdr())
    assert r2.status_code == 200

    list_r = await client.get("/api/clans")
    assert list_r.status_code == 200
    clans = list_r.json()
    assert len(clans) == 1
    assert clans[0]["member_count"] == 1

    detail_r = await client.get(f"/api/clans/{clans[0]['id']}")
    members = detail_r.json()["members"]
    assert len(members) == 1
    assert members[0]["steam_id"] == "999"
    assert members[0]["character_name"] == "NewMember"
