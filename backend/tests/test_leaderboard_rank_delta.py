"""Regression tests for the leaderboard rank-delta feature (see main.py's
GET /api/leaderboard and _leaderboard_snapshot_task). Built after that feature
shipped with zero automated verification — this sandbox has no Python interpreter
for day-to-day work, so `bash scripts/check_backend.sh` (fast import check) and
`uv run --python 3.12 --with-requirements requirements.txt pytest backend/tests`
(this file) are the way to actually execute backend code before trusting it.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.models import PlayerRecord, PlayerRankSnapshot

pytestmark = pytest.mark.asyncio


async def _seed_rank_delta_scenario(db_session):
    now = datetime.now(timezone.utc)
    hist_ts = now - timedelta(days=10)  # past the endpoint's 7-day cutoff
    db_session.add_all([
        # Current standings (server 1): Alice 1st, Bob 2nd, Carol 3rd, Dave 4th (no history)
        PlayerRecord(server_num=1, player_name="Alice", total_seconds=10000, last_seen=now, session_count=5),
        PlayerRecord(server_num=1, player_name="Bob",   total_seconds=5000,  last_seen=now, session_count=5),
        PlayerRecord(server_num=1, player_name="Carol", total_seconds=1000,  last_seen=now, session_count=5),
        PlayerRecord(server_num=1, player_name="Dave",  total_seconds=100,   last_seen=now, session_count=1),
        # Historical snapshot ~10 days ago: Bob was #1, Alice #2, Carol #3
        PlayerRankSnapshot(server_num=1, player_name="Bob",   total_seconds=9000, recorded_at=hist_ts),
        PlayerRankSnapshot(server_num=1, player_name="Alice", total_seconds=8000, recorded_at=hist_ts),
        PlayerRankSnapshot(server_num=1, player_name="Carol", total_seconds=1000, recorded_at=hist_ts),
    ])
    await db_session.commit()


async def test_rank_delta_climbed_dropped_unchanged_and_new_player(client, db_session):
    await _seed_rank_delta_scenario(db_session)
    r = await client.get("/api/leaderboard", params={"server": 1, "period": "all", "page": 1, "per_page": 25})
    assert r.status_code == 200
    deltas = {it["player_name"]: it["rank_delta"] for it in r.json()}
    assert deltas == {"Alice": 1, "Bob": -1, "Carol": 0, "Dave": None}


async def test_rank_delta_is_null_outside_period_all(client, db_session):
    await _seed_rank_delta_scenario(db_session)
    r = await client.get("/api/leaderboard", params={"server": 1, "period": "week", "page": 1, "per_page": 25})
    assert r.status_code == 200
    assert all(it["rank_delta"] is None for it in r.json())


async def test_rank_delta_survives_pagination(client, db_session):
    await _seed_rank_delta_scenario(db_session)
    # per_page=2 puts Carol (#3) and Dave (#4) on page 2 — current_rank must reflect
    # the TRUE overall position, not reset to (1, 2) for the second page.
    r = await client.get("/api/leaderboard", params={"server": 1, "period": "all", "page": 2, "per_page": 2})
    assert r.status_code == 200
    items = r.json()
    assert [it["player_name"] for it in items] == ["Carol", "Dave"]
    assert {it["player_name"]: it["rank_delta"] for it in items} == {"Carol": 0, "Dave": None}


async def test_rank_delta_before_any_snapshot_exists(client, db_session):
    # Simulates day 1 of this feature: PlayerRecord rows exist, but the nightly
    # snapshot task hasn't run yet, so player_rank_snapshots is completely empty
    # for this server. Must not error — every rank_delta should just be null.
    now = datetime.now(timezone.utc)
    db_session.add_all([
        PlayerRecord(server_num=2, player_name="Eve",   total_seconds=500, last_seen=now, session_count=1),
        PlayerRecord(server_num=2, player_name="Frank", total_seconds=200, last_seen=now, session_count=1),
    ])
    await db_session.commit()
    r = await client.get("/api/leaderboard", params={"server": 2, "period": "all", "page": 1, "per_page": 25})
    assert r.status_code == 200
    assert all(it["rank_delta"] is None for it in r.json())


async def test_snapshot_task_writes_current_standings(db_session, db_engine):
    import backend.main as main

    now = datetime.now(timezone.utc)
    db_session.add_all([
        PlayerRecord(server_num=1, player_name="Alice", total_seconds=10000, session_count=5),
        PlayerRecord(server_num=1, player_name="Bob",   total_seconds=5000,  session_count=5),
        PlayerRecord(server_num=2, player_name="Eve",   total_seconds=500,   session_count=1),
    ])
    await db_session.commit()

    # _leaderboard_snapshot_task is an infinite loop gated on asyncio.sleep(until 00:15
    # UTC); make that resolve instantly so its body runs, then stop it after one pass.
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fast_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError()
        await real_sleep(0)

    main.asyncio.sleep = fast_sleep
    task = asyncio.create_task(main._leaderboard_snapshot_task())
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    finally:
        main.asyncio.sleep = real_sleep
        if not task.done():
            task.cancel()

    import backend.database as database
    async with database.AsyncSessionLocal() as db:
        rows = (await db.execute(select(PlayerRankSnapshot))).scalars().all()
    got = sorted((r.server_num, r.player_name, r.total_seconds) for r in rows)
    assert got == sorted([(1, "Alice", 10000), (1, "Bob", 5000), (2, "Eve", 500)])
    assert all(r.recorded_at is not None for r in rows)
