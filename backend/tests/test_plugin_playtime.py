"""Regression tests for GET /api/plugin/playtime — global (all-servers) total playtime for
a linked SteamID, summed directly off PlayerRecord.steam_id (mirrors the same
func.sum(PlayerRecord.total_seconds) aggregation used for a player's public profile total,
which likewise sums across every server_num). See the "Player playtime (plugin)" section
comments in main.py."""
import pytest

from backend.models import PlayerRecord, Setting

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-playtime"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def test_playtime_requires_plugin_key(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/playtime", params={"steam_id": "76500000000000001"})
    assert r.status_code == 401


async def test_playtime_zero_for_unknown_steam_id(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get(
        "/api/plugin/playtime",
        params={"steam_id": "76500000000000001", "server_num": 1},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"total_seconds": 0}


async def test_playtime_sums_across_servers(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76500000000000002"
    db_session.add(PlayerRecord(server_num=1, player_name="Alice", total_seconds=1000, steam_id=steam_id))
    db_session.add(PlayerRecord(server_num=2, player_name="Alice", total_seconds=2500, steam_id=steam_id))
    # Unrelated player/steam_id must not leak into the sum.
    db_session.add(PlayerRecord(server_num=1, player_name="Bob", total_seconds=9999, steam_id="76500000000000099"))
    await db_session.commit()

    r = await client.get(
        "/api/plugin/playtime",
        params={"steam_id": steam_id, "server_num": 1},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"total_seconds": 3500}


async def test_playtime_ignores_unclaimed_records_with_no_steam_id(client, db_session):
    """PlayerRecord rows never claimed by a real session report (steam_id NULL, e.g. old
    A2S-only estimates) must not contribute — the lookup is a direct steam_id match."""
    await _set_plugin_key(db_session)
    db_session.add(PlayerRecord(server_num=1, player_name="Ghost", total_seconds=5000, steam_id=None))
    await db_session.commit()

    r = await client.get(
        "/api/plugin/playtime",
        params={"steam_id": "76500000000000003", "server_num": 1},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"total_seconds": 0}
