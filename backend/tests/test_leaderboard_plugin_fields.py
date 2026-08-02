"""Regression tests for the leaderboard/plugin integration fields added to
GET /api/leaderboard (clan, combat power, online status, connect-streak, day period,
clan_id filter) and the new GET /api/leaderboard/trend endpoint."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.models import GameClan, GameClanMember, PlayerDailyActivity, PlayerRankSnapshot, PlayerRecord

pytestmark = pytest.mark.asyncio


def _day(offset):
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


async def test_clan_power_and_online_populated_for_clan_member(client, db_session):
    clan = GameClan(server_num=1, clan_guid="g-1", name="Кровавые Клыки")
    db_session.add(clan)
    await db_session.commit()
    await db_session.refresh(clan)
    db_session.add(GameClanMember(
        clan_id=clan.id, steam_id="1", character_name="Alice", role="leader",
        is_online=True, physical_power=812.5, spell_power=640.2,
    ))
    db_session.add(PlayerRecord(server_num=1, player_name="Alice", total_seconds=1000, steam_id="1"))
    await db_session.commit()

    r = await client.get("/api/leaderboard", params={"server": 1})
    assert r.status_code == 200
    row = r.json()[0]
    assert row["clan_id"] == clan.id
    assert row["clan_name"] == "Кровавые Клыки"
    assert row["physical_power"] == 812.5
    assert row["spell_power"] == 640.2
    assert row["is_online"] is True


async def test_clan_fields_null_for_non_clan_player(client, db_session):
    db_session.add(PlayerRecord(server_num=1, player_name="Solo", total_seconds=500, steam_id="2"))
    await db_session.commit()
    r = await client.get("/api/leaderboard", params={"server": 1})
    assert r.status_code == 200
    row = r.json()[0]
    assert row["clan_id"] is None
    assert row["clan_name"] is None
    assert row["physical_power"] is None
    assert row["is_online"] is None
    assert row["streak_days"] == 0


async def test_clan_id_filter_only_returns_clan_members(client, db_session):
    clan_a = GameClan(server_num=1, clan_guid="g-a", name="Clan A")
    clan_b = GameClan(server_num=1, clan_guid="g-b", name="Clan B")
    db_session.add_all([clan_a, clan_b])
    await db_session.commit()
    await db_session.refresh(clan_a)
    await db_session.refresh(clan_b)
    db_session.add_all([
        GameClanMember(clan_id=clan_a.id, steam_id="10", character_name="A1", role="leader"),
        GameClanMember(clan_id=clan_b.id, steam_id="20", character_name="B1", role="leader"),
        PlayerRecord(server_num=1, player_name="A1", total_seconds=100, steam_id="10"),
        PlayerRecord(server_num=1, player_name="B1", total_seconds=200, steam_id="20"),
        PlayerRecord(server_num=1, player_name="NoClan", total_seconds=300, steam_id="30"),
    ])
    await db_session.commit()

    r = await client.get("/api/leaderboard", params={"server": 1, "clan_id": clan_a.id})
    assert r.status_code == 200
    names = [row["player_name"] for row in r.json()]
    assert names == ["A1"]


async def test_clan_id_filter_unknown_clan_returns_empty(client, db_session):
    db_session.add(PlayerRecord(server_num=1, player_name="Someone", total_seconds=100, steam_id="99"))
    await db_session.commit()
    r = await client.get("/api/leaderboard", params={"server": 1, "clan_id": 99999})
    assert r.status_code == 200
    assert r.json() == []


async def test_streak_counts_consecutive_days_ending_today(client, db_session):
    db_session.add(PlayerRecord(server_num=1, player_name="Streaker", total_seconds=100, steam_id="5"))
    db_session.add_all([
        PlayerDailyActivity(server_num=1, steam_id="5", activity_date=_day(0)),
        PlayerDailyActivity(server_num=1, steam_id="5", activity_date=_day(1)),
        PlayerDailyActivity(server_num=1, steam_id="5", activity_date=_day(2)),
        PlayerDailyActivity(server_num=1, steam_id="5", activity_date=_day(10)),  # gap — not part of the streak
    ])
    await db_session.commit()
    r = await client.get("/api/leaderboard", params={"server": 1})
    assert r.status_code == 200
    assert r.json()[0]["streak_days"] == 3


async def test_streak_still_counts_if_not_yet_played_today(client, db_session):
    # Grace period: streak shouldn't reset to 0 just because the clock rolled over UTC
    # midnight and the player hasn't logged in again yet today.
    db_session.add(PlayerRecord(server_num=1, player_name="Yesterday", total_seconds=100, steam_id="6"))
    db_session.add_all([
        PlayerDailyActivity(server_num=1, steam_id="6", activity_date=_day(1)),
        PlayerDailyActivity(server_num=1, steam_id="6", activity_date=_day(2)),
    ])
    await db_session.commit()
    r = await client.get("/api/leaderboard", params={"server": 1})
    assert r.status_code == 200
    assert r.json()[0]["streak_days"] == 2


async def test_day_period_filters_by_last_24h(client, db_session):
    now = datetime.now(timezone.utc)
    db_session.add_all([
        PlayerRecord(server_num=1, player_name="Recent", total_seconds=100, last_seen=now),
        PlayerRecord(server_num=1, player_name="Stale", total_seconds=200, last_seen=now - timedelta(days=5)),
    ])
    await db_session.commit()
    r = await client.get("/api/leaderboard", params={"server": 1, "period": "day"})
    assert r.status_code == 200
    names = [row["player_name"] for row in r.json()]
    assert names == ["Recent"]


async def test_trend_endpoint_returns_daily_deltas(client, db_session):
    now = datetime.now(timezone.utc)
    db_session.add_all([
        PlayerRankSnapshot(server_num=1, player_name="TrendPlayer", total_seconds=1000, recorded_at=now - timedelta(days=2)),
        PlayerRankSnapshot(server_num=1, player_name="TrendPlayer", total_seconds=1500, recorded_at=now - timedelta(days=1)),
        PlayerRankSnapshot(server_num=1, player_name="TrendPlayer", total_seconds=2200, recorded_at=now),
    ])
    await db_session.commit()
    r = await client.get("/api/leaderboard/trend", params={"player_name": "TrendPlayer", "server": 1, "days": 7})
    assert r.status_code == 200
    seconds = [row["seconds"] for row in r.json()]
    assert seconds == [500, 700]


async def test_trend_endpoint_unknown_player_returns_empty(client, db_session):
    r = await client.get("/api/leaderboard/trend", params={"player_name": "Nobody", "server": 1})
    assert r.status_code == 200
    assert r.json() == []
