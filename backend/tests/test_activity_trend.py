"""Regression tests for GET /api/users/{username}/activity-trend — derives daily
playtime from PlayerRankSnapshot's nightly cumulative total_seconds copies (see
backend/routers/users.py's docstring for why a diff-of-cumulative-totals approach)."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import get_password_hash
from backend.models import PlayerRankSnapshot, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, game_nickname=None):
    user = User(
        username=username, email=f"{username}@example.com",
        hashed_password=get_password_hash("x"), role="user", game_nickname=game_nickname,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _day(offset):
    return (datetime.now(timezone.utc) - timedelta(days=offset)).replace(hour=3, minute=0, second=0, microsecond=0)


async def test_returns_daily_deltas_from_cumulative_snapshots(client, db_session):
    await _make_user(db_session, "Alice")
    db_session.add_all([
        PlayerRankSnapshot(server_num=1, player_name="Alice", total_seconds=1000, recorded_at=_day(3)),
        PlayerRankSnapshot(server_num=1, player_name="Alice", total_seconds=1500, recorded_at=_day(2)),
        PlayerRankSnapshot(server_num=1, player_name="Alice", total_seconds=1500, recorded_at=_day(1)),
        PlayerRankSnapshot(server_num=1, player_name="Alice", total_seconds=2200, recorded_at=_day(0)),
    ])
    await db_session.commit()
    r = await client.get("/api/users/Alice/activity-trend", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    seconds_by_day = [row["seconds"] for row in body]
    # First snapshot (day 3) is only ever a baseline — 3 deltas from 4 snapshots.
    assert seconds_by_day == [500, 0, 700]


async def test_sums_across_multiple_servers_same_day(client, db_session):
    await _make_user(db_session, "Bob")
    db_session.add_all([
        PlayerRankSnapshot(server_num=1, player_name="Bob", total_seconds=1000, recorded_at=_day(1)),
        PlayerRankSnapshot(server_num=2, player_name="Bob", total_seconds=500, recorded_at=_day(1)),
        PlayerRankSnapshot(server_num=1, player_name="Bob", total_seconds=1300, recorded_at=_day(0)),
        PlayerRankSnapshot(server_num=2, player_name="Bob", total_seconds=600, recorded_at=_day(0)),
    ])
    await db_session.commit()
    r = await client.get("/api/users/Bob/activity-trend", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["seconds"] == 400  # (1300-1000) + (600-500)


async def test_negative_delta_clamped_to_zero_not_negative(client, db_session):
    # A wipe (or manual admin adjustment) can reset total_seconds downward —
    # must never show as negative playtime.
    await _make_user(db_session, "Carol")
    db_session.add_all([
        PlayerRankSnapshot(server_num=1, player_name="Carol", total_seconds=5000, recorded_at=_day(1)),
        PlayerRankSnapshot(server_num=1, player_name="Carol", total_seconds=100, recorded_at=_day(0)),
    ])
    await db_session.commit()
    r = await client.get("/api/users/Carol/activity-trend", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert body[0]["seconds"] == 0


async def test_uses_game_nickname_when_linked(client, db_session):
    await _make_user(db_session, "SiteHandle", game_nickname="InGameName")
    db_session.add_all([
        PlayerRankSnapshot(server_num=1, player_name="InGameName", total_seconds=100, recorded_at=_day(1)),
        PlayerRankSnapshot(server_num=1, player_name="InGameName", total_seconds=400, recorded_at=_day(0)),
    ])
    await db_session.commit()
    r = await client.get("/api/users/SiteHandle/activity-trend", params={"days": 7})
    assert r.status_code == 200
    assert r.json()[0]["seconds"] == 300


async def test_fewer_than_two_snapshots_returns_empty_list(client, db_session):
    await _make_user(db_session, "Dave")
    db_session.add(PlayerRankSnapshot(server_num=1, player_name="Dave", total_seconds=100, recorded_at=_day(0)))
    await db_session.commit()
    r = await client.get("/api/users/Dave/activity-trend", params={"days": 7})
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_username_404s(client, db_session):
    r = await client.get("/api/users/NoSuchPlayer/activity-trend")
    assert r.status_code == 404


async def test_inactive_user_404s(client, db_session):
    user = await _make_user(db_session, "Deactivated")
    user.is_active = False
    await db_session.commit()
    r = await client.get("/api/users/Deactivated/activity-trend")
    assert r.status_code == 404
