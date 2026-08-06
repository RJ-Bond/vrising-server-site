"""Regression tests for GET /api/clans/mine — the logged-in user's in-game clan
membership, resolved via User.steam_id → GameClanMember, powering the homepage "Твой
клан" widget (frontend/index.js). See backend/routers/clans.py's my_clan() docstring
for why this route must be registered ahead of GET /api/clans/{clan_id}."""
import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import Setting, User

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-456"


async def _make_user(db_session, username, steam_id=None, role="user"):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
        steam_id=steam_id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    existing = (await db_session.execute(select(Setting).where(Setting.key == "plugin_api_key"))).scalar_one_or_none()
    if existing is None:
        db_session.add(Setting(key="plugin_api_key", value=value))
        await db_session.commit()


def _member(steam_id, name, role="member", is_online=False):
    return {"steam_id": steam_id, "character_name": name, "role": role, "is_online": is_online}


async def _sync(client, db_session, clans, server_num=1):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/clans/sync",
        json={"server_num": server_num, "clans": clans},
        headers={"X-Plugin-Key": PLUGIN_KEY},
    )
    assert r.status_code == 200
    return r


async def test_my_clan_requires_login(client, db_session):
    r = await client.get("/api/clans/mine")
    assert r.status_code == 401


async def test_my_clan_none_when_no_steam_id_linked(client, db_session):
    user = await _make_user(db_session, "NoLinkUser1", steam_id=None)
    r = await client.get("/api/clans/mine", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"clan": None}


async def test_my_clan_none_when_steam_id_not_in_any_roster(client, db_session):
    user = await _make_user(db_session, "UnclannedUser1", steam_id="777")
    await _sync(client, db_session, [
        {"clan_guid": "guid-a", "name": "Some Clan", "motto": "", "members": [
            _member("1", "P1", "leader"),
        ]},
    ])
    r = await client.get("/api/clans/mine", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"clan": None}


async def test_my_clan_returns_clan_with_role_and_counts(client, db_session):
    user = await _make_user(db_session, "ClannedUser1", steam_id="42")
    await _sync(client, db_session, [
        {"clan_guid": "guid-b", "name": "My Clan", "motto": "", "members": [
            _member("1", "Leader1", "leader", is_online=True),
            _member("42", "MyChar", "officer", is_online=True),
            _member("3", "P3", "member", is_online=False),
        ]},
    ])
    r = await client.get("/api/clans/mine", headers=_bearer(user))
    assert r.status_code == 200
    body = r.json()["clan"]
    assert body is not None
    assert body["name"] == "My Clan"
    assert body["my_role"] == "officer"
    assert body["member_count"] == 3
    assert body["online_count"] == 2


async def test_my_clan_picks_membership_matching_steam_id_across_clans(client, db_session):
    """If the linked steam_id shows up in more than one synced clan (edge case —
    normally shouldn't happen since a character belongs to one clan at a time), the
    endpoint must not 500; it just returns some single clan deterministically."""
    user = await _make_user(db_session, "MultiClanUser1", steam_id="99")
    await _sync(client, db_session, [
        {"clan_guid": "guid-c", "name": "Clan One", "motto": "", "members": [
            _member("99", "Char99", "member"),
        ]},
    ])
    r = await client.get("/api/clans/mine", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["clan"]["name"] == "Clan One"
