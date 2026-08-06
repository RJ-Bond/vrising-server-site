"""Regression tests for GET /api/clans/leaderboard — ranks clans by total combat
power (physical_power+spell_power summed across the FULL roster, not the 4-member
preview GET /api/clans exposes), with member/online counts riding along as secondary
stats. See backend/routers/clans.py's clans_leaderboard() docstring for why this route
must be registered ahead of GET /api/clans/{clan_id}."""
import pytest
from sqlalchemy import select

from backend.models import Setting

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    # Idempotent — _sync() below calls this on every sync, and several tests sync
    # more than once (different server_num, replacement rosters, etc.), which would
    # otherwise hit settings.key's UNIQUE constraint on the second insert.
    existing = (await db_session.execute(select(Setting).where(Setting.key == "plugin_api_key"))).scalar_one_or_none()
    if existing is None:
        db_session.add(Setting(key="plugin_api_key", value=value))
        await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


def _member(steam_id, name, role="member", is_online=False, physical_power=None, spell_power=None):
    return {
        "steam_id": steam_id, "character_name": name, "role": role,
        "is_online": is_online, "physical_power": physical_power, "spell_power": spell_power,
    }


async def _sync(client, db_session, clans, server_num=1):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/clans/sync",
        json={"server_num": server_num, "clans": clans},
        headers=_hdr(),
    )
    assert r.status_code == 200
    return r


async def test_ranked_by_total_power_across_full_roster_not_just_preview(client, db_session):
    """A clan with 5 low-power members should still outrank a clan with 1 very-high-power
    member if its FULL-roster sum is bigger — and since GET /api/clans's member_preview
    caps at 4, this also proves the leaderboard isn't just re-using that truncated list."""
    await _sync(client, db_session, [
        {
            "clan_guid": "guid-wide", "name": "Wide Clan", "motto": "",
            "members": [
                _member("1", "W1", "leader", physical_power=100, spell_power=100),
                _member("2", "W2", physical_power=100, spell_power=100),
                _member("3", "W3", physical_power=100, spell_power=100),
                _member("4", "W4", physical_power=100, spell_power=100),
                _member("5", "W5", physical_power=100, spell_power=100),
            ],
        },
        {
            "clan_guid": "guid-narrow", "name": "Narrow Clan", "motto": "",
            "members": [
                _member("6", "N1", "leader", physical_power=400, spell_power=400),
            ],
        },
    ])

    r = await client.get("/api/clans/leaderboard")
    assert r.status_code == 200
    rows = r.json()
    assert [row["name"] for row in rows] == ["Wide Clan", "Narrow Clan"]
    assert rows[0]["total_power"] == pytest.approx(1000.0)
    assert rows[0]["member_count"] == 5
    assert rows[0]["avg_power"] == pytest.approx(200.0)
    assert rows[1]["total_power"] == pytest.approx(800.0)


async def test_null_power_counts_as_zero_not_excluded(client, db_session):
    """UnlinkedWanderer-style members (never spawned a character, so no
    StoredPowerStats yet) must not be dropped from member_count, just contribute 0."""
    await _sync(client, db_session, [
        {
            "clan_guid": "guid-a", "name": "A Clan", "motto": "",
            "members": [
                _member("1", "P1", "leader", physical_power=50, spell_power=50),
                _member("2", "P2"),  # no power fields at all
            ],
        },
    ])
    r = await client.get("/api/clans/leaderboard")
    row = r.json()[0]
    assert row["member_count"] == 2
    assert row["total_power"] == pytest.approx(100.0)
    assert row["avg_power"] == pytest.approx(50.0)


async def test_online_count_reflects_is_online_members(client, db_session):
    await _sync(client, db_session, [
        {
            "clan_guid": "guid-a", "name": "A Clan", "motto": "",
            "members": [
                _member("1", "P1", "leader", is_online=True),
                _member("2", "P2", is_online=True),
                _member("3", "P3", is_online=False),
            ],
        },
    ])
    r = await client.get("/api/clans/leaderboard")
    row = r.json()[0]
    assert row["member_count"] == 3
    assert row["online_count"] == 2


async def test_zero_member_clans_are_excluded(client, db_session):
    await _sync(client, db_session, [
        {"clan_guid": "guid-empty", "name": "Empty Clan", "motto": "", "members": []},
        {"clan_guid": "guid-real", "name": "Real Clan", "motto": "", "members": [
            _member("1", "P1", "leader", physical_power=10, spell_power=10),
        ]},
    ])
    r = await client.get("/api/clans/leaderboard")
    names = [row["name"] for row in r.json()]
    assert names == ["Real Clan"]


async def test_server_param_scopes_to_one_server(client, db_session):
    await _sync(client, db_session, [
        {"clan_guid": "guid-s1", "name": "Server1 Clan", "motto": "", "members": [
            _member("1", "P1", "leader", physical_power=10, spell_power=10),
        ]},
    ], server_num=1)
    await _sync(client, db_session, [
        {"clan_guid": "guid-s2", "name": "Server2 Clan", "motto": "", "members": [
            _member("2", "P2", "leader", physical_power=999, spell_power=999),
        ]},
    ], server_num=2)

    r = await client.get("/api/clans/leaderboard", params={"server": 1})
    assert r.status_code == 200
    names = [row["name"] for row in r.json()]
    assert names == ["Server1 Clan"]

    r_all = await client.get("/api/clans/leaderboard")
    names_all = {row["name"] for row in r_all.json()}
    assert names_all == {"Server1 Clan", "Server2 Clan"}


async def test_limit_caps_results_and_max_limit_is_enforced(client, db_session):
    clans = [
        {"clan_guid": f"guid-{i}", "name": f"Clan {i}", "motto": "", "members": [
            _member(str(i), f"P{i}", "leader", physical_power=float(i), spell_power=0),
        ]}
        for i in range(5)
    ]
    await _sync(client, db_session, clans)

    r = await client.get("/api/clans/leaderboard", params={"limit": 2})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # Highest power (Clan 4, Clan 3) should be the ones kept.
    assert [row["name"] for row in rows] == ["Clan 4", "Clan 3"]

    # A limit above the hard cap (_LEADERBOARD_MAX_LIMIT=50) is clamped, not honored
    # verbatim — with only 5 clans synced this just confirms the request doesn't 422 or
    # error, the real ceiling would need 50+ clans to observe directly.
    r_big = await client.get("/api/clans/leaderboard", params={"limit": 9999})
    assert r_big.status_code == 200
    assert len(r_big.json()) == 5


async def test_leaderboard_route_not_shadowed_by_clan_id_route(client, db_session):
    """Regression guard: /api/clans/leaderboard must resolve to this endpoint, not 422
    from GET /api/clans/{clan_id}'s int path converter trying (and failing) to parse
    the literal string "leaderboard"."""
    await _sync(client, db_session, [
        {"clan_guid": "guid-a", "name": "A Clan", "motto": "", "members": [
            _member("1", "P1", "leader"),
        ]},
    ])
    r = await client.get("/api/clans/leaderboard")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
