"""Regression tests for POST /api/plugin/connect-streak — records today (site-local
calendar date, Setting "timezone") as active for a steam_id+server_num and returns the
current consecutive-day connect streak. See PlayerDailyActivity in backend/models.py and
the "Connect streak (plugin)" section in backend/main.py."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.models import PlayerDailyActivity, Setting

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-streak"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    # Pin the site timezone to UTC (rather than relying on the Europe/Moscow default)
    # so test dates computed off datetime.now(timezone.utc) can never disagree with the
    # endpoint's site-local "today" near a UTC midnight boundary — avoids a rare flake.
    db_session.add(Setting(key="timezone", value="UTC"))
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


def _days_ago_str(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


async def test_requires_plugin_key(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/connect-streak", json={"steam_id": "111", "server_num": 1})
    assert r.status_code == 401


async def test_first_ever_connect_returns_streak_of_one(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/connect-streak",
        json={"steam_id": "76561198000000001", "server_num": 1},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json() == {"streak_days": 1}


async def test_second_connect_same_day_is_idempotent(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76561198000000002"

    r1 = await client.post(
        "/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr()
    )
    r2 = await client.post(
        "/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr()
    )
    assert r1.json() == {"streak_days": 1}
    assert r2.json() == {"streak_days": 1}

    rows = await db_session.execute(
        select(PlayerDailyActivity).where(
            PlayerDailyActivity.server_num == 1,
            PlayerDailyActivity.steam_id == steam_id,
        )
    )
    assert len(rows.scalars().all()) == 1


async def test_three_consecutive_prior_days_plus_today_gives_streak_of_four(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76561198000000003"
    for n in (1, 2, 3):
        db_session.add(PlayerDailyActivity(server_num=1, steam_id=steam_id, activity_date=_days_ago_str(n)))
    await db_session.commit()

    r = await client.post(
        "/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr()
    )
    assert r.status_code == 200
    assert r.json() == {"streak_days": 4}


async def test_gap_before_today_resets_streak_to_one(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76561198000000004"
    # Activity 3 days ago only — nothing yesterday or the day before, so today's connect
    # must not chain onto that isolated old day.
    db_session.add(PlayerDailyActivity(server_num=1, steam_id=steam_id, activity_date=_days_ago_str(3)))
    await db_session.commit()

    r = await client.post(
        "/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr()
    )
    assert r.status_code == 200
    assert r.json() == {"streak_days": 1}


async def test_streaks_are_independent_per_server(client, db_session):
    await _set_plugin_key(db_session)
    steam_id = "76561198000000005"
    for n in (1, 2, 3):
        db_session.add(PlayerDailyActivity(server_num=1, steam_id=steam_id, activity_date=_days_ago_str(n)))
    await db_session.commit()

    r_server1 = await client.post(
        "/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 1}, headers=_hdr()
    )
    r_server2 = await client.post(
        "/api/plugin/connect-streak", json={"steam_id": steam_id, "server_num": 2}, headers=_hdr()
    )
    assert r_server1.json() == {"streak_days": 4}
    assert r_server2.json() == {"streak_days": 1}
