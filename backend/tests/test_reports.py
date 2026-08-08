"""Regression tests for backend/routers/reports.py: user-facing report creation
(comment/user/news) and the moderator-only review queue (list + patch). See the
Report model in models.py and ReportCreate/ReportReview in schemas.py."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import Report, User

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


async def _make_report(db_session, reporter, target_type="comment", target_id=1, reason="spam", status="pending"):
    r = Report(reporter_id=reporter.id, target_type=target_type, target_id=target_id, reason=reason, status=status)
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


# ─── POST /api/reports ────────────────────────────────────────────────────────

async def test_create_report_requires_auth(client, db_session):
    r = await client.post(
        "/api/reports",
        json={"target_type": "comment", "target_id": 1, "reason": "spam"},
    )
    assert r.status_code == 401


async def test_create_report_happy_path(client, db_session):
    user = await _make_user(db_session, "Reporter1")
    r = await client.post(
        "/api/reports",
        json={"target_type": "comment", "target_id": 5, "reason": "Оскорбления"},
        headers=_bearer(user),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ok"] is True
    assert "id" in body


async def test_create_report_rejects_invalid_target_type(client, db_session):
    user = await _make_user(db_session, "Reporter2")
    r = await client.post(
        "/api/reports",
        json={"target_type": "bogus", "target_id": 1, "reason": "spam"},
        headers=_bearer(user),
    )
    assert r.status_code == 422


# ─── GET /api/admin/reports ───────────────────────────────────────────────────

async def test_list_reports_requires_moderator(client, db_session):
    user = await _make_user(db_session, "PlainUser1")
    r = await client.get("/api/admin/reports", headers=_bearer(user))
    assert r.status_code == 403


async def test_list_reports_rejects_unauthenticated(client, db_session):
    r = await client.get("/api/admin/reports")
    assert r.status_code == 401


async def test_list_reports_happy_path_for_moderator(client, db_session):
    reporter = await _make_user(db_session, "Reporter3")
    moderator = await _make_user(db_session, "Mod1", role="moderator")
    await _make_report(db_session, reporter, reason="Реклама")

    r = await client.get("/api/admin/reports", headers=_bearer(moderator))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["reason"] == "Реклама"


async def test_list_reports_filters_by_status(client, db_session):
    reporter = await _make_user(db_session, "Reporter4")
    moderator = await _make_user(db_session, "Mod2", role="moderator")
    await _make_report(db_session, reporter, reason="Pending one", status="pending")
    await _make_report(db_session, reporter, reason="Reviewed one", status="reviewed")

    r = await client.get("/api/admin/reports", params={"status": "reviewed"}, headers=_bearer(moderator))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["reason"] == "Reviewed one"


# ─── PATCH /api/admin/reports/{id} ────────────────────────────────────────────

async def test_review_report_requires_moderator(client, db_session):
    reporter = await _make_user(db_session, "Reporter5")
    user = await _make_user(db_session, "PlainUser2")
    report = await _make_report(db_session, reporter)

    r = await client.patch(
        f"/api/admin/reports/{report.id}",
        json={"status": "reviewed"},
        headers=_bearer(user),
    )
    assert r.status_code == 403


async def test_review_report_happy_path(client, db_session):
    reporter = await _make_user(db_session, "Reporter6")
    moderator = await _make_user(db_session, "Mod3", role="moderator")
    report = await _make_report(db_session, reporter)

    r = await client.patch(
        f"/api/admin/reports/{report.id}",
        json={"status": "dismissed", "admin_note": "Не нарушение"},
        headers=_bearer(moderator),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    await db_session.refresh(report)
    assert report.status == "dismissed"
    assert report.admin_note == "Не нарушение"
    assert report.reviewed_at is not None


async def test_review_report_404_for_missing(client, db_session):
    moderator = await _make_user(db_session, "Mod4", role="moderator")
    r = await client.patch(
        "/api/admin/reports/99999",
        json={"status": "reviewed"},
        headers=_bearer(moderator),
    )
    assert r.status_code == 404


async def test_review_report_rejects_invalid_status(client, db_session):
    reporter = await _make_user(db_session, "Reporter7")
    moderator = await _make_user(db_session, "Mod5", role="moderator")
    report = await _make_report(db_session, reporter)

    r = await client.patch(
        f"/api/admin/reports/{report.id}",
        json={"status": "bogus"},
        headers=_bearer(moderator),
    )
    assert r.status_code == 422
