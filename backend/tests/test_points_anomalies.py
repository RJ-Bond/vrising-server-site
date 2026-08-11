"""Regression tests for GET /api/admin/points/anomalies (backend/routers/points_shop.py)
— the abuse/integrity companion to GET /api/admin/points/diagnostics. See that
endpoint's own docstring for what each of the three checks (large_single_sessions,
impossible_daily_rate, burst_activity) means and why "same Steam ID / IP across many
accounts" isn't attempted (schema doesn't support it)."""
from datetime import datetime, timedelta

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import PointsTransaction, Setting, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _make_tx(db_session, user, reason, created_at, delta=10):
    tx = PointsTransaction(user_id=user.id, delta=delta, balance_after=delta, reason=reason, created_at=created_at)
    db_session.add(tx)
    await db_session.commit()
    return tx


async def test_anomalies_requires_admin(client, db_session):
    user = await _make_user(db_session, "PlainUser")
    r = await client.get("/api/admin/points/anomalies", headers=_bearer(user))
    assert r.status_code == 403


async def test_anomalies_zero_state_has_no_findings(client, db_session):
    admin = await _make_user(db_session, "AnomAdmin1", role="admin")
    r = await client.get("/api/admin/points/anomalies", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["anomaly_count"] == 0
    assert d["large_single_sessions"] == []
    assert d["impossible_daily_rate"] == []
    assert d["burst_activity"] == []


async def test_anomalies_flags_large_single_session(client, db_session):
    """points_per_minute_playtime defaults to 1 (no Setting row seeded), so a single
    playtime award of 800 points implies an 800-minute (>12h) session — over the
    720-minute _ANOMALY_LARGE_SESSION_MINUTES threshold."""
    admin = await _make_user(db_session, "AnomAdmin2", role="admin")
    player = await _make_user(db_session, "HugeSessionPlayer")
    now = datetime.utcnow()
    await _make_tx(db_session, player, "playtime", now - timedelta(hours=1), delta=800)

    r = await client.get("/api/admin/points/anomalies", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert len(d["large_single_sessions"]) == 1
    row = d["large_single_sessions"][0]
    assert row["username"] == "HugeSessionPlayer"
    assert row["implied_minutes"] == 800
    assert d["anomaly_count"] == 1


async def test_anomalies_flags_impossible_daily_rate_without_any_single_outlier(client, db_session):
    """Three separate playtime awards of 500 each (all below the 720-minute
    single-session threshold) on the same calendar day sum to 1500 implied minutes —
    over the 1440-minute (24h) _ANOMALY_IMPOSSIBLE_DAILY_MINUTES threshold. Verifies the
    daily aggregate check catches session-splitting that the per-row check alone would
    miss."""
    admin = await _make_user(db_session, "AnomAdmin3", role="admin")
    player = await _make_user(db_session, "SplitSessionPlayer")
    day = datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
    await _make_tx(db_session, player, "playtime", day, delta=500)
    await _make_tx(db_session, player, "playtime", day + timedelta(hours=2), delta=500)
    await _make_tx(db_session, player, "playtime", day + timedelta(hours=4), delta=500)

    r = await client.get("/api/admin/points/anomalies", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["large_single_sessions"] == []
    assert len(d["impossible_daily_rate"]) == 1
    row = d["impossible_daily_rate"][0]
    assert row["username"] == "SplitSessionPlayer"
    assert row["implied_minutes"] == 1500
    assert row["awarded_points"] == 1500


async def test_anomalies_flags_burst_activity(client, db_session):
    """Eight small playtime awards for one user inside the same clock hour trips the
    burst-frequency threshold (_ANOMALY_BURST_TX_PER_HOUR=8), independent of the
    per-session/daily point totals (each delta here is tiny)."""
    admin = await _make_user(db_session, "AnomAdmin4", role="admin")
    player = await _make_user(db_session, "ReconnectSpamPlayer")
    hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    for i in range(8):
        await _make_tx(db_session, player, "playtime", hour + timedelta(minutes=i), delta=1)

    r = await client.get("/api/admin/points/anomalies", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["large_single_sessions"] == []
    assert d["impossible_daily_rate"] == []
    assert len(d["burst_activity"]) == 1
    row = d["burst_activity"][0]
    assert row["username"] == "ReconnectSpamPlayer"
    assert row["tx_count"] == 8


async def test_anomalies_respects_days_window(client, db_session):
    """A large single-session award outside the lookback window must not be flagged."""
    admin = await _make_user(db_session, "AnomAdmin5", role="admin")
    player = await _make_user(db_session, "OldOutlierPlayer")
    await _make_tx(db_session, player, "playtime", datetime.utcnow() - timedelta(days=20), delta=900)

    r = await client.get("/api/admin/points/anomalies?days=7", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["window_days"] == 7
    assert d["anomaly_count"] == 0


async def test_anomalies_rate_checks_disabled_when_per_minute_is_zero(client, db_session):
    """When points_per_minute_playtime is configured to 0 (earning temporarily turned
    off), implied-minutes math would divide by zero — the rate-based checks
    (large_single_sessions/impossible_daily_rate) must skip cleanly rather than error,
    while burst_activity (a pure transaction-count signal) still runs."""
    admin = await _make_user(db_session, "AnomAdmin6", role="admin")
    player = await _make_user(db_session, "ZeroRatePlayer")
    db_session.add(Setting(key="points_per_minute_playtime", value="0"))
    await db_session.commit()
    hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    for i in range(9):
        await _make_tx(db_session, player, "playtime", hour + timedelta(minutes=i), delta=0)

    r = await client.get("/api/admin/points/anomalies", headers=_bearer(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["large_single_sessions"] == []
    assert d["impossible_daily_rate"] == []
    assert len(d["burst_activity"]) == 1
