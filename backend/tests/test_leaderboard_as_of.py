"""Regression + behavior tests for GET /api/leaderboard's `as_of` query param (see
backend/routers/leaderboard.py) — reconstructs the leaderboard from PlayerRankSnapshot
instead of live PlayerRecord totals, using each player's closest-prior snapshot (the
same _closest_prior_snapshot_totals() helper the rank-delta indicator already used,
now shared instead of duplicated). Also covers GET /api/leaderboard/snapshot-range,
the small companion endpoint the frontend date picker uses to know how far back it can
go.
"""
from datetime import datetime, timezone

import pytest

from backend.models import PlayerRecord, PlayerRankSnapshot

pytestmark = pytest.mark.asyncio


async def _seed_as_of_scenario(db_session):
    # Live/current standings are deliberately different from every snapshot below, so
    # asserting on `as_of` values actually proves the read came from PlayerRankSnapshot
    # and not (accidentally) from live PlayerRecord.
    db_session.add_all([
        PlayerRecord(server_num=1, player_name="Alice", total_seconds=10000, session_count=9, steam_id="1"),
        PlayerRecord(server_num=1, player_name="Bob",   total_seconds=5000,  session_count=7, steam_id="2"),
        # Snapshot on 2026-01-05
        PlayerRankSnapshot(server_num=1, player_name="Alice", total_seconds=3000, recorded_at=datetime(2026, 1, 5, 0, 15, tzinfo=timezone.utc)),
        PlayerRankSnapshot(server_num=1, player_name="Bob",   total_seconds=1000, recorded_at=datetime(2026, 1, 5, 0, 15, tzinfo=timezone.utc)),
        # Snapshot on 2026-01-10
        PlayerRankSnapshot(server_num=1, player_name="Alice", total_seconds=6000, recorded_at=datetime(2026, 1, 10, 0, 15, tzinfo=timezone.utc)),
        PlayerRankSnapshot(server_num=1, player_name="Bob",   total_seconds=4000, recorded_at=datetime(2026, 1, 10, 0, 15, tzinfo=timezone.utc)),
    ])
    await db_session.commit()


async def test_as_of_omitted_matches_live_behavior(client, db_session):
    """Regression guard: omitting `as_of` must still read from live PlayerRecord
    totals, completely unaffected by whatever's in PlayerRankSnapshot — byte-for-byte
    the same behavior as before this param existed."""
    await _seed_as_of_scenario(db_session)
    r = await client.get("/api/leaderboard", params={"server": 1, "period": "all", "page": 1, "per_page": 25})
    assert r.status_code == 200
    totals = {it["player_name"]: it["total_seconds"] for it in r.json()}
    assert totals == {"Alice": 10000, "Bob": 5000}


async def test_as_of_exact_snapshot_date(client, db_session):
    await _seed_as_of_scenario(db_session)
    r = await client.get("/api/leaderboard", params={"server": 1, "as_of": "2026-01-05"})
    assert r.status_code == 200
    items = r.json()
    assert [it["player_name"] for it in items] == ["Alice", "Bob"]  # ranked by that date's totals
    totals = {it["player_name"]: it["total_seconds"] for it in items}
    assert totals == {"Alice": 3000, "Bob": 1000}
    # Historical view: no rank-delta relative to today's live standings.
    assert all(it["rank_delta"] is None for it in items)


async def test_as_of_between_two_snapshots_uses_closest_prior(client, db_session):
    await _seed_as_of_scenario(db_session)
    r = await client.get("/api/leaderboard", params={"server": 1, "as_of": "2026-01-08"})
    assert r.status_code == 200
    totals = {it["player_name"]: it["total_seconds"] for it in r.json()}
    # 2026-01-08 falls between the 01-05 and 01-10 snapshots — the closest PRIOR one
    # (01-05) must be used, not the later one and not an interpolation between them.
    assert totals == {"Alice": 3000, "Bob": 1000}


async def test_as_of_before_any_snapshot_returns_empty(client, db_session):
    await _seed_as_of_scenario(db_session)
    r = await client.get("/api/leaderboard", params={"server": 1, "as_of": "2025-12-01"})
    assert r.status_code == 200
    assert r.json() == []


async def test_as_of_ignores_period_and_respects_search(client, db_session):
    await _seed_as_of_scenario(db_session)
    # `period` is meaningless for a cumulative snapshot total — must not filter
    # anything out or error when combined with `as_of`.
    r = await client.get("/api/leaderboard", params={"server": 1, "period": "day", "as_of": "2026-01-10"})
    assert r.status_code == 200
    assert {it["player_name"] for it in r.json()} == {"Alice", "Bob"}

    r2 = await client.get("/api/leaderboard", params={"server": 1, "as_of": "2026-01-10", "q": "ali"})
    assert r2.status_code == 200
    assert [it["player_name"] for it in r2.json()] == ["Alice"]


async def test_as_of_survives_pagination(client, db_session):
    await _seed_as_of_scenario(db_session)
    r = await client.get("/api/leaderboard", params={"server": 1, "as_of": "2026-01-10", "page": 2, "per_page": 1})
    assert r.status_code == 200
    items = r.json()
    assert [it["player_name"] for it in items] == ["Bob"]
    assert items[0]["total_seconds"] == 4000


async def test_snapshot_range_endpoint(client, db_session):
    r_empty = await client.get("/api/leaderboard/snapshot-range", params={"server": 1})
    assert r_empty.status_code == 200
    assert r_empty.json() == {"earliest_date": None}

    await _seed_as_of_scenario(db_session)
    r = await client.get("/api/leaderboard/snapshot-range", params={"server": 1})
    assert r.status_code == 200
    assert r.json() == {"earliest_date": "2026-01-05"}
