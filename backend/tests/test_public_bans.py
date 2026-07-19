"""Regression tests for GET /api/bans — a public, unauthenticated list of currently-
active in-game bans for bans.html's anonymous visitors. character_name and reason ARE
included deliberately: in-game server bans are ordinary server-transparency content
(like a public rules-violations board), not sensitive personal data — there's no real
name, email, or other PII involved, just a game character name.

Replaces the former test_public_ban_count.py, written back when this endpoint was a
privacy-conscious count-only summary ({"active_bans": N}); that instinct turned out not
to match what's actually wanted for this page, so the endpoint (and these tests) moved
to a full list, same row shape as GET /api/admin/bans's active view minus
steam_id/unbanned_at/id-only-for-admin-unban concerns — id is still included since
bans.html reuses it for the admin-only "Разбанить" button on this same public table."""
from datetime import datetime, timedelta

import pytest

from backend.models import Ban

pytestmark = pytest.mark.asyncio


async def _make_ban(db_session, **kwargs):
    defaults = dict(
        server_num=1, steam_id="76561198000000001", character_name="Griefer",
        admin_name="AdminOne", reason="test reason", banned_at=datetime.utcnow(),
        unban_at=None, unbanned_at=None,
    )
    defaults.update(kwargs)
    ban = Ban(**defaults)
    db_session.add(ban)
    await db_session.commit()
    await db_session.refresh(ban)
    return ban


async def test_no_bans_returns_empty_list(client):
    r = await client.get("/api/bans")
    assert r.status_code == 200
    assert r.json() == {"bans": []}


async def test_returns_only_active_bans_not_lifted_ones(client, db_session):
    await _make_ban(db_session, steam_id="1", character_name="Active1")
    await _make_ban(db_session, steam_id="2", character_name="Active2")
    await _make_ban(db_session, steam_id="3", character_name="Lifted", unbanned_at=datetime.utcnow())

    r = await client.get("/api/bans")
    assert r.status_code == 200
    names = [b["character_name"] for b in r.json()["bans"]]
    assert set(names) == {"Active1", "Active2"}


async def test_no_auth_required(client, db_session):
    await _make_ban(db_session, steam_id="1")
    # No Authorization header at all — must still work, this is a public endpoint.
    r = await client.get("/api/bans")
    assert r.status_code == 200
    assert len(r.json()["bans"]) == 1


async def test_response_includes_character_name_and_reason(client, db_session):
    """Deliberate reversal of the old privacy-conscious count-only behavior — this data
    is meant to be public."""
    await _make_ban(db_session, steam_id="1", character_name="VisibleName", reason="visible reason")
    r = await client.get("/api/bans")
    body = r.json()
    assert len(body["bans"]) == 1
    b = body["bans"][0]
    assert b["character_name"] == "VisibleName"
    assert b["reason"] == "visible reason"
    assert "VisibleName" in r.text
    assert "visible reason" in r.text


async def test_response_shape_matches_admin_active_bans_minus_steam_id(client, db_session):
    ban = await _make_ban(
        db_session, steam_id="76561198000000002", character_name="Shapey",
        admin_name="Overseer", reason="cheating", server_num=1,
    )
    r = await client.get("/api/bans")
    b = r.json()["bans"][0]
    assert set(b.keys()) == {
        "id", "server_num", "server_name", "character_name", "admin_name", "reason",
        "banned_at", "unban_at",
    }
    assert b["id"] == ban.id
    assert "steam_id" not in b
    assert "unbanned_at" not in b


async def test_permanent_ban_has_null_unban_at(client, db_session):
    await _make_ban(db_session, steam_id="1", unban_at=None)
    r = await client.get("/api/bans")
    assert r.json()["bans"][0]["unban_at"] is None


async def test_temp_ban_unban_at_is_iso_z(client, db_session):
    future = datetime.utcnow() + timedelta(hours=2)
    await _make_ban(db_session, steam_id="1", unban_at=future)
    r = await client.get("/api/bans")
    unban_at = r.json()["bans"][0]["unban_at"]
    assert unban_at is not None
    assert unban_at.endswith("Z")


async def test_most_recent_first(client, db_session):
    now = datetime.utcnow()
    await _make_ban(db_session, steam_id="older", character_name="Older", banned_at=now - timedelta(hours=1))
    await _make_ban(db_session, steam_id="newer", character_name="Newer", banned_at=now)
    r = await client.get("/api/bans")
    names = [b["character_name"] for b in r.json()["bans"]]
    assert names == ["Newer", "Older"]


async def test_server_name_falls_back_when_unset(client, db_session):
    await _make_ban(db_session, steam_id="1", server_num=7)
    r = await client.get("/api/bans")
    assert r.json()["bans"][0]["server_name"] == "Сервер 7"
