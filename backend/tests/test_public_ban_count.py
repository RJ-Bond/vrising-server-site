"""Regression tests for GET /api/bans — a public, unauthenticated, privacy-safe count
of active in-game bans for bans.html's anonymous visitors. Repurposed from a former
paginated listing of banned SITE accounts (removed from the frontend, leaving the old
endpoint orphaned) to give the page some public value without exposing character names,
ban reasons, or which specific players are banned."""
from datetime import datetime

import pytest

from backend.models import Ban

pytestmark = pytest.mark.asyncio


async def _make_ban(db_session, **kwargs):
    defaults = dict(
        server_num=1, steam_id="76561198000000001", character_name="Griefer",
        admin_name="AdminOne", reason="test", banned_at=datetime.utcnow(),
        unban_at=None, unbanned_at=None,
    )
    defaults.update(kwargs)
    ban = Ban(**defaults)
    db_session.add(ban)
    await db_session.commit()
    return ban


async def test_no_bans_returns_zero(client):
    r = await client.get("/api/bans")
    assert r.status_code == 200
    assert r.json() == {"active_bans": 0}


async def test_counts_only_active_bans_not_lifted_ones(client, db_session):
    await _make_ban(db_session, steam_id="1")
    await _make_ban(db_session, steam_id="2")
    await _make_ban(db_session, steam_id="3", unbanned_at=datetime.utcnow())  # lifted

    r = await client.get("/api/bans")
    assert r.status_code == 200
    assert r.json() == {"active_bans": 2}


async def test_no_auth_required(client, db_session):
    await _make_ban(db_session, steam_id="1")
    # No Authorization header at all — must still work, this is a public endpoint.
    r = await client.get("/api/bans")
    assert r.status_code == 200
    assert r.json()["active_bans"] == 1


async def test_response_never_includes_player_identifying_fields(client, db_session):
    await _make_ban(db_session, steam_id="1", character_name="SensitiveName", reason="secret reason")
    r = await client.get("/api/bans")
    body = r.json()
    assert list(body.keys()) == ["active_bans"]
    assert "SensitiveName" not in r.text
    assert "secret reason" not in r.text
