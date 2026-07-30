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
    # Both clans need >=1 member — 0-member clans are excluded from GET /api/clans
    # entirely (see test_public_list_hides_empty_clans_and_sorts_by_member_count), and
    # this test is only about the search filter, not that behavior.
    body = {
        "server_num": 1,
        "clans": [
            {"clan_guid": "guid-a", "name": "Blood Fangs", "motto": "", "members": [
                {"steam_id": "1", "character_name": "P1", "role": "leader"},
            ]},
            {"clan_guid": "guid-b", "name": "Night Watch", "motto": "", "members": [
                {"steam_id": "2", "character_name": "P2", "role": "leader"},
            ]},
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


async def test_clan_list_includes_member_preview_leaders_first_capped_at_four(client, db_session):
    """member_preview backs the public clan-list card's mini avatar-stack — leaders/
    officers should sort first, and it should never exceed 4 entries even for a clan
    with more members (member_count still reflects the true total)."""
    await _set_plugin_key(db_session)
    linked_user = User(
        username="LinkedLeader", email="ll@example.com",
        hashed_password=get_password_hash("x"), steam_id="1",
        avatar_url="https://example.com/a.png",
    )
    db_session.add(linked_user)
    await db_session.commit()

    body = {
        "server_num": 1,
        "clans": [{
            "clan_guid": "guid-big",
            "name": "Big Clan",
            "motto": "",
            "members": [
                {"steam_id": "5", "character_name": "Zebra", "role": "member"},
                {"steam_id": "4", "character_name": "Yak", "role": "member"},
                {"steam_id": "3", "character_name": "Xenon", "role": "member"},
                {"steam_id": "2", "character_name": "Officer1", "role": "officer"},
                {"steam_id": "1", "character_name": "TheLeader", "role": "leader"},
            ],
        }],
    }
    await client.post("/api/plugin/clans/sync", json=body, headers=_hdr())

    r = await client.get("/api/clans")
    clan = r.json()[0]
    assert clan["member_count"] == 5
    preview = clan["member_preview"]
    assert len(preview) == 4  # capped, even though the clan has 5 members
    assert preview[0]["character_name"] == "TheLeader"
    assert preview[0]["role"] == "leader"
    assert preview[0]["username"] == "LinkedLeader"
    assert preview[0]["avatar_url"] == "https://example.com/a.png"
    assert preview[1]["character_name"] == "Officer1"
    assert preview[1]["role"] == "officer"
    # remaining two slots filled alphabetically from the plain members
    assert [m["character_name"] for m in preview[2:]] == ["Xenon", "Yak"]


async def test_public_list_hides_empty_clans_and_sorts_by_member_count(client, db_session):
    """Production regression: V Rising lets anyone spin up a clan for free, so a large
    share of game-synced clans have 0 members (abandoned/throwaway) — those shouldn't
    clutter the public showcase. The remaining ones should lead with the biggest (most
    real) clans, not alphabetically — plain alphabetical sort put junk like "1"/"123"
    ahead of every active community since digits sort before letters."""
    await _set_plugin_key(db_session)
    body = {
        "server_num": 1,
        "clans": [
            {"clan_guid": "guid-empty", "name": "1", "motto": "", "members": []},
            {"clan_guid": "guid-small", "name": "Small Clan", "motto": "", "members": [
                {"steam_id": "1", "character_name": "P1", "role": "leader"},
            ]},
            {"clan_guid": "guid-big", "name": "Big Clan", "motto": "", "members": [
                {"steam_id": "2", "character_name": "P2", "role": "leader"},
                {"steam_id": "3", "character_name": "P3", "role": "officer"},
                {"steam_id": "4", "character_name": "P4", "role": "member"},
            ]},
        ],
    }
    await client.post("/api/plugin/clans/sync", json=body, headers=_hdr())

    r = await client.get("/api/clans")
    assert r.status_code == 200
    clans = r.json()
    names = [c["name"] for c in clans]
    assert "1" not in names  # 0-member clan excluded entirely
    assert names == ["Big Clan", "Small Clan"]  # biggest first
    assert all(c["member_count"] > 0 for c in clans)


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


async def test_sync_online_status_power_and_bases_round_trip(client, db_session):
    """Regression test for the plugin's newer payload fields (is_online,
    last_connected_unix, physical_power/spell_power per member, and the clan's `bases`
    array) — send them on sync, then confirm GET /api/clans and GET /api/clans/{id} both
    read them back."""
    await _set_plugin_key(db_session)

    body = {
        "server_num": 1,
        "clans": [
            {
                "clan_guid": "guid-alpha",
                "name": "Alpha Clan",
                "motto": "First!",
                "members": [
                    {
                        "steam_id": "111", "character_name": "AlphaLeader", "role": "leader",
                        "is_online": True, "last_connected_unix": 1732900000,
                        "physical_power": 123.5, "spell_power": 87.25,
                    },
                    {
                        "steam_id": "222", "character_name": "AlphaMember", "role": "member",
                        "is_online": False, "last_connected_unix": 1732800000,
                        "physical_power": None, "spell_power": None,
                    },
                ],
                "bases": [
                    {
                        "level": 4, "floor_count": 3, "is_raid_protected": True,
                        "min_x": -100, "min_z": -50, "max_x": 100, "max_z": 50,
                    },
                ],
            },
        ],
    }
    r = await client.post("/api/plugin/clans/sync", json=body, headers=_hdr())
    assert r.status_code == 200

    list_r = await client.get("/api/clans")
    assert list_r.status_code == 200
    clan = list_r.json()[0]
    assert len(clan["bases"]) == 1
    assert clan["bases"][0] == {
        "level": 4, "floor_count": 3, "is_raid_protected": True,
        "min_x": -100, "min_z": -50, "max_x": 100, "max_z": 50,
    }
    preview = {m["steam_id"]: m for m in clan["member_preview"]}
    assert preview["111"]["is_online"] is True
    assert preview["111"]["last_connected_unix"] == 1732900000
    assert preview["111"]["physical_power"] == 123.5
    assert preview["111"]["spell_power"] == 87.25
    assert preview["222"]["is_online"] is False
    assert preview["222"]["physical_power"] is None

    detail_r = await client.get(f"/api/clans/{clan['id']}")
    assert detail_r.status_code == 200
    detail = detail_r.json()
    assert len(detail["bases"]) == 1
    assert detail["bases"][0]["level"] == 4
    members = {m["steam_id"]: m for m in detail["members"]}
    assert members["111"]["is_online"] is True
    assert members["111"]["physical_power"] == 123.5
    assert members["222"]["is_online"] is False


async def test_sync_without_new_fields_defaults_sanely(client, db_session):
    """Older/interim plugin builds may not send is_online/last_connected_unix/power/bases
    at all — PluginClanMemberIn's defaults must keep the sync from 422ing, and the read
    side should come back with the documented defaults (False/0/None/[])."""
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/clans/sync", json=_sync_body(), headers=_hdr())
    assert r.status_code == 200

    list_r = await client.get("/api/clans")
    clan = list_r.json()[0]
    assert clan["bases"] == []
    for m in clan["member_preview"]:
        assert m["is_online"] is False
        assert m["physical_power"] is None
        assert m["spell_power"] is None


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
