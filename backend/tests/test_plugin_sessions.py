"""Regression tests for POST /api/plugin/sessions — the BepInEx plugin's per-disconnect
session report, gated by the shared plugin_api_key secret. Covers the upsert/claim logic
that merges verified steam_id identity onto pre-existing A2S-only PlayerRecord rows."""
import pytest
from sqlalchemy import select

from backend.models import Setting, PlayerRecord

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def test_session_report_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000001", "character_name": "Alice", "session_seconds": 100},
    )
    assert r.status_code == 401


async def test_session_report_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000001", "character_name": "Alice", "session_seconds": 100},
        headers=_hdr("not-the-real-key"),
    )
    assert r.status_code == 401


async def test_first_session_creates_new_record(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000001", "character_name": "Alice", "session_seconds": 1200},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["success"] is True

    result = await db_session.execute(select(PlayerRecord).where(PlayerRecord.steam_id == "76500000000000001"))
    rows = result.scalars().all()
    assert len(rows) == 1
    rec = rows[0]
    assert rec.player_name == "Alice"
    assert rec.total_seconds == 1200
    assert rec.session_count == 1
    assert rec.last_duration == 1200
    assert rec.last_seen is not None


async def test_second_session_accumulates(client, db_session):
    await _set_plugin_key(db_session)
    await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000002", "character_name": "Bob", "session_seconds": 500},
        headers=_hdr(),
    )
    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000002", "character_name": "Bob", "session_seconds": 300},
        headers=_hdr(),
    )
    assert r.status_code == 200

    result = await db_session.execute(select(PlayerRecord).where(PlayerRecord.steam_id == "76500000000000002"))
    rows = result.scalars().all()
    assert len(rows) == 1
    rec = rows[0]
    assert rec.total_seconds == 800
    assert rec.session_count == 2
    assert rec.last_duration == 300


async def test_a2s_only_row_gets_claimed_not_duplicated(client, db_session):
    await _set_plugin_key(db_session)
    # Pre-existing A2S-only row (never claimed by a real session report).
    db_session.add(PlayerRecord(
        server_num=1,
        player_name="Carol",
        steam_id=None,
        total_seconds=5000,
        last_duration=100,
        session_count=3,
    ))
    await db_session.commit()

    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000003", "character_name": "Carol", "session_seconds": 600},
        headers=_hdr(),
    )
    assert r.status_code == 200

    result = await db_session.execute(select(PlayerRecord).where(PlayerRecord.server_num == 1))
    rows = result.scalars().all()
    assert len(rows) == 1
    rec = rows[0]
    assert rec.steam_id == "76500000000000003"
    assert rec.player_name == "Carol"
    # Prior A2S-accumulated total is preserved and added to, not reset.
    assert rec.total_seconds == 5600
    assert rec.session_count == 4
    assert rec.last_duration == 600


async def test_character_rename_updates_existing_claimed_row(client, db_session):
    await _set_plugin_key(db_session)
    await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000004", "character_name": "Dave", "session_seconds": 100},
        headers=_hdr(),
    )
    r = await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000004", "character_name": "DaveTheGreat", "session_seconds": 200},
        headers=_hdr(),
    )
    assert r.status_code == 200

    result = await db_session.execute(select(PlayerRecord).where(PlayerRecord.steam_id == "76500000000000004"))
    rows = result.scalars().all()
    assert len(rows) == 1
    rec = rows[0]
    assert rec.player_name == "DaveTheGreat"
    assert rec.total_seconds == 300
    assert rec.session_count == 2


async def test_two_different_steam_ids_produce_two_rows(client, db_session):
    await _set_plugin_key(db_session)
    await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000005", "character_name": "Erin", "session_seconds": 100},
        headers=_hdr(),
    )
    await client.post(
        "/api/plugin/sessions",
        json={"server_num": 1, "steam_id": "76500000000000006", "character_name": "Frank", "session_seconds": 100},
        headers=_hdr(),
    )

    result = await db_session.execute(
        select(PlayerRecord).where(PlayerRecord.steam_id.in_(["76500000000000005", "76500000000000006"]))
    )
    rows = result.scalars().all()
    assert len(rows) == 2
    steam_ids = {r.steam_id for r in rows}
    assert steam_ids == {"76500000000000005", "76500000000000006"}
