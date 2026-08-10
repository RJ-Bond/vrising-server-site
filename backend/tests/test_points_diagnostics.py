"""Regression tests for GET /api/admin/points/diagnostics (backend/routers/points_shop.py)
— the admin-panel "is automatic point-earning actually working?" check, built after a
support question where playtime/streak points appeared to have stopped. See that
endpoint's own docstring for why linked-vs-unlinked active players and recent-award
recency are the two signals it surfaces."""
from datetime import datetime, timedelta

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import PlayerRecord, PointsTransaction, User

pytestmark = pytest.mark.asyncio


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


async def _make_player_record(db_session, steam_id, last_seen, name=None):
    rec = PlayerRecord(
        server_num=1,
        player_name=name or f"Player{steam_id}",
        steam_id=steam_id,
        total_seconds=3600,
        last_seen=last_seen,
        session_count=1,
    )
    db_session.add(rec)
    await db_session.commit()
    return rec


async def _make_tx(db_session, user, reason, created_at, delta=10):
    tx = PointsTransaction(user_id=user.id, delta=delta, balance_after=delta, reason=reason, created_at=created_at)
    db_session.add(tx)
    await db_session.commit()
    return tx


async def test_diagnostics_requires_admin(client, db_session):
    user = await _make_user(db_session, "PlainUser")
    r = await client.get("/api/admin/points/diagnostics", headers=_bearer(user))
    assert r.status_code == 403


async def test_diagnostics_counts_linked_vs_unlinked_active_players(client, db_session):
    admin = await _make_user(db_session, "DiagAdmin", role="admin")
    linked_user = await _make_user(db_session, "LinkedPlayer", steam_id="76500000000000201")
    now = datetime.utcnow()
    await _make_player_record(db_session, linked_user.steam_id, last_seen=now)
    await _make_player_record(db_session, "76500000000000999", last_seen=now, name="GhostPlayer")

    r = await client.get("/api/admin/points/diagnostics", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["active_players_14d"] == 2
    assert d["active_players_linked_14d"] == 1
    assert d["active_players_unlinked_14d"] == 1


async def test_diagnostics_excludes_stale_player_records(client, db_session):
    admin = await _make_user(db_session, "DiagAdmin2", role="admin")
    stale_user = await _make_user(db_session, "StalePlayer", steam_id="76500000000000202")
    await _make_player_record(db_session, stale_user.steam_id, last_seen=datetime.utcnow() - timedelta(days=30))

    r = await client.get("/api/admin/points/diagnostics", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["active_players_14d"] == 0
    assert d["active_players_linked_14d"] == 0


async def test_diagnostics_reports_award_totals_and_7d_recency(client, db_session):
    admin = await _make_user(db_session, "DiagAdmin3", role="admin")
    user = await _make_user(db_session, "AwardedPlayer", steam_id="76500000000000203")
    now = datetime.utcnow()
    # One recent playtime award (inside the 7-day window) and one stale one (outside it).
    await _make_tx(db_session, user, "playtime", now - timedelta(days=1))
    await _make_tx(db_session, user, "playtime", now - timedelta(days=20))
    await _make_tx(db_session, user, "streak", now - timedelta(days=15))

    r = await client.get("/api/admin/points/diagnostics", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["playtime_awards_total"] == 2
    assert d["playtime_awards_last_7d"] == 1
    assert d["playtime_awards_last_at"] is not None
    assert d["streak_awards_total"] == 1
    assert d["streak_awards_last_7d"] == 0
    assert d["streak_awards_last_at"] is not None


async def test_diagnostics_zero_state_has_no_awards_and_null_timestamps(client, db_session):
    admin = await _make_user(db_session, "DiagAdmin4", role="admin")

    r = await client.get("/api/admin/points/diagnostics", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["playtime_awards_total"] == 0
    assert d["playtime_awards_last_at"] is None
    assert d["streak_awards_total"] == 0
    assert d["streak_awards_last_at"] is None
    assert d["active_players_14d"] == 0


async def test_diagnostics_includes_current_earning_rate_settings(client, db_session):
    admin = await _make_user(db_session, "DiagAdmin5", role="admin")

    r = await client.get("/api/admin/points/diagnostics", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    # Defaults per _get_points_config's fallback (no Setting rows seeded in this test's DB).
    assert d["points_per_minute_playtime"] == 1
    assert d["points_streak_bonus"] == 10
    assert d["points_streak_min_days"] == 2
