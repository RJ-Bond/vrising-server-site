"""Regression tests for GET /api/clans-embed — the server-rendered <head> meta used for
crawlers that don't execute JS (Discord/Telegram/VK/Twitter unfurlers, most search
bots), mirroring test_events_embed.py's structure for GET /api/events-embed. Without
this, a shared clans.html?clan=<id> link always showed the generic clans-list
title/description/image no matter which clan it was."""
from pathlib import Path

import pytest
from sqlalchemy import select

import backend.routers.clans as clans_router
from backend.models import GameClan, Setting

pytestmark = pytest.mark.asyncio

_REAL_CLANS_HTML = str(Path(__file__).resolve().parent.parent.parent / "frontend" / "clans.html")

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    # Idempotent — mirrors test_clans_leaderboard.py's helper, since settings.key has a
    # UNIQUE constraint and a test might sync more than once.
    existing = (await db_session.execute(select(Setting).where(Setting.key == "plugin_api_key"))).scalar_one_or_none()
    if existing is None:
        db_session.add(Setting(key="plugin_api_key", value=value))
        await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


def _member(steam_id, name, role="member", physical_power=None, spell_power=None):
    return {
        "steam_id": steam_id, "character_name": name, "role": role,
        "physical_power": physical_power, "spell_power": spell_power,
    }


async def _sync_clan(client, db_session, clan_guid, name, members, server_num=1):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/clans/sync",
        json={"server_num": server_num, "clans": [
            {"clan_guid": clan_guid, "name": name, "motto": "", "members": members},
        ]},
        headers=_hdr(),
    )
    assert r.status_code == 200
    clan = (await db_session.execute(select(GameClan).where(GameClan.clan_guid == clan_guid))).scalar_one()
    return clan


async def test_clans_embed_swaps_clan_meta(client, db_session, monkeypatch):
    monkeypatch.setattr(clans_router, "_CLANS_HTML_PATH", _REAL_CLANS_HTML)

    clan = await _sync_clan(client, db_session, "guid-embed", "Кровавые Клыки", [
        _member("1", "P1", "leader", physical_power=100, spell_power=50),
        _member("2", "P2", physical_power=50, spell_power=0),
    ])

    r = await client.get("/api/clans-embed", params={"id": clan.id})
    assert r.status_code == 200
    body = r.text
    assert "Кровавые Клыки" in body
    assert f"/clans.html?clan={clan.id}" in body
    assert "участников: 2" in body
    # The default clans-list meta must actually be gone, not just appended alongside it.
    assert "Кланы V Rising — Just-Skill.Ru</title>" not in body


async def test_clans_embed_unknown_id_falls_back_to_default_meta(client, db_session, monkeypatch):
    monkeypatch.setattr(clans_router, "_CLANS_HTML_PATH", _REAL_CLANS_HTML)

    r = await client.get("/api/clans-embed", params={"id": 999999})
    assert r.status_code == 200
    assert "Кланы V Rising — Just-Skill.Ru</title>" in r.text


async def test_clans_embed_missing_clans_html_returns_404(client, db_session, monkeypatch):
    monkeypatch.setattr(clans_router, "_CLANS_HTML_PATH", "/nonexistent/clans.html")

    r = await client.get("/api/clans-embed", params={"id": 1})
    assert r.status_code == 404
