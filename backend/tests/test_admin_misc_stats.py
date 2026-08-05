"""Regression tests for the stats/analytics/export/audit-log endpoints in
backend/routers/admin_misc.py: GET /api/admin/stats (dashboard counters),
GET /api/admin/audit-log(+/actions), GET /api/admin/analytics,
GET /api/admin/economy-stats, the CSV export endpoints (users/audit-log/bans),
and GET /api/admin/errors. Each is admin-gated (export/bans is moderator-gated)
and checked for correct shape and role enforcement."""
import csv
import io
from datetime import datetime, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import AuditLog, Comment, ErrorLog, News, PageView, PointsTransaction, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user", points_balance=0):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
        points_balance=points_balance,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _make_news(db_session, author, slug="post", title="Post"):
    news = News(title=title, slug=slug, summary="s", content="c", author_id=author.id)
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    return news


# ─── GET /api/admin/stats ────────────────────────────────────────────────────

async def test_admin_stats_requires_admin(client, db_session):
    user = await _make_user(db_session, "StatsUser1", role="user")
    r = await client.get("/api/admin/stats", headers=_bearer(user))
    assert r.status_code == 403


async def test_admin_stats_returns_counts(client, db_session):
    admin = await _make_user(db_session, "StatsAdmin1", role="admin")
    news = await _make_news(db_session, admin, slug="stats-post")
    db_session.add(Comment(news_id=news.id, author_id=admin.id, content="Hi there"))
    await db_session.commit()

    r = await client.get("/api/admin/stats", headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["user_count"] >= 1
    assert body["news_count"] == 1
    assert body["comment_count"] == 1
    assert len(body["recent_comments"]) == 1
    assert body["recent_comments"][0]["news_slug"] == "stats-post"


# ─── GET /api/admin/audit-log & /actions ────────────────────────────────────

async def test_audit_log_requires_admin(client, db_session):
    user = await _make_user(db_session, "AuditUser1", role="user")
    r = await client.get("/api/admin/audit-log", headers=_bearer(user))
    assert r.status_code == 403


async def test_audit_log_lists_entries_and_filters_by_action(client, db_session):
    admin = await _make_user(db_session, "AuditAdmin1", role="admin")
    db_session.add(AuditLog(admin_username=admin.username, action="event.create", detail="Made an event"))
    db_session.add(AuditLog(admin_username=admin.username, action="event.delete", detail="Removed an event"))
    await db_session.commit()

    r = await client.get("/api/admin/audit-log", headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2

    r2 = await client.get("/api/admin/audit-log", params={"action": "event.create"}, headers=_bearer(admin))
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["total"] == 1
    assert body2["items"][0]["action"] == "event.create"


async def test_audit_log_search_by_query(client, db_session):
    admin = await _make_user(db_session, "AuditAdmin2", role="admin")
    db_session.add(AuditLog(admin_username=admin.username, action="user.ban", detail="Banned SneakyPlayer"))
    db_session.add(AuditLog(admin_username=admin.username, action="user.warn", detail="Warned NicePlayer"))
    await db_session.commit()

    r = await client.get("/api/admin/audit-log", params={"q": "Sneaky"}, headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert "Sneaky" in body["items"][0]["detail"]


async def test_audit_log_actions_returns_distinct_list(client, db_session):
    admin = await _make_user(db_session, "AuditAdmin3", role="admin")
    db_session.add(AuditLog(admin_username=admin.username, action="event.create", detail="a"))
    db_session.add(AuditLog(admin_username=admin.username, action="event.create", detail="b"))
    db_session.add(AuditLog(admin_username=admin.username, action="wipe.create", detail="c"))
    await db_session.commit()

    r = await client.get("/api/admin/audit-log/actions", headers=_bearer(admin))
    assert r.status_code == 200
    actions = r.json()
    assert sorted(actions) == ["event.create", "wipe.create"]


# ─── GET /api/admin/analytics ────────────────────────────────────────────────

async def test_analytics_requires_admin(client, db_session):
    user = await _make_user(db_session, "AnalyticsUser1", role="user")
    r = await client.get("/api/admin/analytics", headers=_bearer(user))
    assert r.status_code == 403


async def test_analytics_returns_expected_shape_and_totals(client, db_session):
    admin = await _make_user(db_session, "AnalyticsAdmin1", role="admin")
    await _make_news(db_session, admin, slug="analytics-post")
    now = datetime.now(timezone.utc)
    db_session.add(PageView(path="/news/analytics-post", ip_hash="abc123", created_at=now))
    db_session.add(PageView(path="/news/analytics-post", ip_hash="abc123", created_at=now))
    db_session.add(PageView(path="/shop", ip_hash="def456", created_at=now))
    await db_session.commit()

    r = await client.get("/api/admin/analytics", params={"days": 7}, headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["total_views"] == 3
    assert body["totals"]["users"] >= 1
    assert body["totals"]["news"] == 1
    paths = {p["path"] for p in body["top_pages"]}
    assert "/news/analytics-post" in paths
    assert "top_news" in body
    assert "users_by_day" in body


# ─── GET /api/admin/economy-stats ────────────────────────────────────────────

async def test_economy_stats_requires_admin(client, db_session):
    user = await _make_user(db_session, "EconUser1", role="user")
    r = await client.get("/api/admin/economy-stats", headers=_bearer(user))
    assert r.status_code == 403


async def test_economy_stats_aggregates_issued_and_spent(client, db_session):
    admin = await _make_user(db_session, "EconAdmin1", role="admin")
    user = await _make_user(db_session, "EconTarget1", points_balance=50)
    db_session.add(PointsTransaction(user_id=user.id, delta=100, balance_after=100, reason="playtime"))
    db_session.add(PointsTransaction(user_id=user.id, delta=-50, balance_after=50, reason="redeem"))
    await db_session.commit()

    r = await client.get("/api/admin/economy-stats", headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["balance_total"] >= 50
    total_issued = sum(d["issued"] for d in body["by_day"])
    total_spent = sum(d["spent"] for d in body["by_day"])
    assert total_issued == 100
    assert total_spent == 50


# ─── CSV export endpoints ────────────────────────────────────────────────────

async def test_export_users_requires_admin(client, db_session):
    user = await _make_user(db_session, "ExportUser1", role="user")
    r = await client.get("/api/admin/export/users", headers=_bearer(user))
    assert r.status_code == 403


async def test_export_users_returns_csv_with_header_and_rows(client, db_session):
    admin = await _make_user(db_session, "ExportAdmin1", role="admin")
    await _make_user(db_session, "ExportTarget1", role="user")

    r = await client.get("/api/admin/export/users", headers=_bearer(admin))
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["id", "username", "email", "role", "is_active", "created_at", "last_active_at"]
    usernames = [row[1] for row in rows[1:]]
    assert "ExportAdmin1" in usernames
    assert "ExportTarget1" in usernames


async def test_export_audit_log_returns_csv(client, db_session):
    admin = await _make_user(db_session, "ExportAdmin2", role="admin")
    db_session.add(AuditLog(admin_username=admin.username, action="event.create", detail="did a thing"))
    await db_session.commit()

    r = await client.get("/api/admin/export/audit-log", headers=_bearer(admin))
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["id", "admin_username", "action", "target_type", "target_id", "detail", "created_at"]
    assert any(row[2] == "event.create" for row in rows[1:])


async def test_export_bans_allows_moderator_and_excludes_active_users(client, db_session):
    moderator = await _make_user(db_session, "ModExport1", role="moderator")
    banned = await _make_user(db_session, "BannedUser1", role="user")
    banned.is_active = False
    await db_session.commit()
    await _make_user(db_session, "ActiveUser1", role="user")

    r = await client.get("/api/admin/export/bans", headers=_bearer(moderator))
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))
    usernames = [row[1] for row in rows[1:]]
    assert "BannedUser1" in usernames
    assert "ActiveUser1" not in usernames


async def test_export_bans_requires_moderator(client, db_session):
    user = await _make_user(db_session, "PlainExportUser1", role="user")
    r = await client.get("/api/admin/export/bans", headers=_bearer(user))
    assert r.status_code == 403


# ─── GET /api/admin/errors ───────────────────────────────────────────────────

async def test_error_log_requires_admin(client, db_session):
    user = await _make_user(db_session, "ErrorUser1", role="user")
    r = await client.get("/api/admin/errors", headers=_bearer(user))
    assert r.status_code == 403


async def test_error_log_lists_entries_and_filters_by_query(client, db_session):
    admin = await _make_user(db_session, "ErrorAdmin1", role="admin")
    db_session.add(ErrorLog(path="/api/broken", method="GET", status_code=500, error="Boom"))
    db_session.add(ErrorLog(path="/api/fine", method="GET", status_code=404, error="Not found"))
    await db_session.commit()

    r = await client.get("/api/admin/errors", headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2

    r2 = await client.get("/api/admin/errors", params={"q": "Boom"}, headers=_bearer(admin))
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["total"] == 1
    assert body2["items"][0]["path"] == "/api/broken"
