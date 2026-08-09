"""Regression tests for AutoFlagRule: the admin CRUD in backend/routers/reports.py
and the auto-flagging hook (_check_auto_flag_rules) fired from
POST /api/news/{slug}/comments in backend/routers/news.py. A matching active rule
must create a Report against the new comment (surfaced in the normal moderation
queue) without blocking/rejecting the comment itself."""
import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import AutoFlagRule, News, Report, User

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


async def _make_news(db_session, author, slug="post"):
    news = News(title="Post", slug=slug, summary="s", content="c", author_id=author.id, published=True)
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    return news


async def _make_rule(db_session, keyword, is_active=True, created_by="Mod"):
    rule = AutoFlagRule(keyword=keyword, is_active=is_active, created_by=created_by)
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)
    return rule


# ─── CRUD: GET/POST/PATCH/DELETE /api/admin/auto-flag-rules ─────────────────────

async def test_list_rules_requires_moderator(client, db_session):
    user = await _make_user(db_session, "PlainUser1")
    r = await client.get("/api/admin/auto-flag-rules", headers=_bearer(user))
    assert r.status_code == 403


async def test_create_rule_happy_path(client, db_session):
    moderator = await _make_user(db_session, "Mod1", role="moderator")
    r = await client.post(
        "/api/admin/auto-flag-rules", json={"keyword": "spamword"}, headers=_bearer(moderator)
    )
    assert r.status_code == 201
    body = r.json()
    assert body["keyword"] == "spamword"
    assert body["is_active"] is True
    assert body["created_by"] == "Mod1"


async def test_create_rule_rejects_empty_keyword(client, db_session):
    moderator = await _make_user(db_session, "Mod2", role="moderator")
    r = await client.post("/api/admin/auto-flag-rules", json={"keyword": "   "}, headers=_bearer(moderator))
    assert r.status_code == 422


async def test_list_rules_returns_created(client, db_session):
    moderator = await _make_user(db_session, "Mod3", role="moderator")
    await _make_rule(db_session, "badword1")
    await _make_rule(db_session, "badword2", is_active=False)
    r = await client.get("/api/admin/auto-flag-rules", headers=_bearer(moderator))
    assert r.status_code == 200
    keywords = {item["keyword"] for item in r.json()}
    assert {"badword1", "badword2"} <= keywords


async def test_update_rule_toggles_active(client, db_session):
    moderator = await _make_user(db_session, "Mod4", role="moderator")
    rule = await _make_rule(db_session, "toggleme")
    r = await client.patch(
        f"/api/admin/auto-flag-rules/{rule.id}", json={"is_active": False}, headers=_bearer(moderator)
    )
    assert r.status_code == 200
    assert r.json()["is_active"] is False


async def test_update_rule_404_for_missing(client, db_session):
    moderator = await _make_user(db_session, "Mod5", role="moderator")
    r = await client.patch(
        "/api/admin/auto-flag-rules/999999", json={"is_active": False}, headers=_bearer(moderator)
    )
    assert r.status_code == 404


async def test_delete_rule_happy_path(client, db_session):
    moderator = await _make_user(db_session, "Mod6", role="moderator")
    rule = await _make_rule(db_session, "deleteme")
    r = await client.delete(f"/api/admin/auto-flag-rules/{rule.id}", headers=_bearer(moderator))
    assert r.status_code == 204
    remaining = (await db_session.execute(select(AutoFlagRule).where(AutoFlagRule.id == rule.id))).scalar_one_or_none()
    assert remaining is None


async def test_delete_rule_404_for_missing(client, db_session):
    moderator = await _make_user(db_session, "Mod7", role="moderator")
    r = await client.delete("/api/admin/auto-flag-rules/999999", headers=_bearer(moderator))
    assert r.status_code == 404


# ─── Comment-creation hook ───────────────────────────────────────────────────────

async def test_comment_matching_active_keyword_creates_report(client, db_session):
    author = await _make_user(db_session, "AuthorFlag1")
    await _make_news(db_session, author, slug="flag-post-1")
    await _make_rule(db_session, "scam")
    commenter = await _make_user(db_session, "CommenterFlag1")

    r = await client.post(
        "/api/news/flag-post-1/comments",
        json={"content": "this looks like a scam to me"},
        headers=_bearer(commenter),
    )
    assert r.status_code == 201  # comment is created, never blocked
    comment_id = r.json()["id"]

    reports = (await db_session.execute(
        select(Report).where(Report.target_type == "comment", Report.target_id == comment_id)
    )).scalars().all()
    assert len(reports) == 1
    assert "scam" in reports[0].reason
    assert reports[0].reporter_id is None


async def test_comment_not_matching_any_keyword_creates_no_report(client, db_session):
    author = await _make_user(db_session, "AuthorFlag2")
    await _make_news(db_session, author, slug="flag-post-2")
    await _make_rule(db_session, "scam")
    commenter = await _make_user(db_session, "CommenterFlag2")

    r = await client.post(
        "/api/news/flag-post-2/comments",
        json={"content": "totally normal, friendly comment"},
        headers=_bearer(commenter),
    )
    assert r.status_code == 201
    comment_id = r.json()["id"]

    reports = (await db_session.execute(
        select(Report).where(Report.target_type == "comment", Report.target_id == comment_id)
    )).scalars().all()
    assert reports == []


async def test_inactive_rule_keyword_does_not_trigger(client, db_session):
    author = await _make_user(db_session, "AuthorFlag3")
    await _make_news(db_session, author, slug="flag-post-3")
    await _make_rule(db_session, "quietword", is_active=False)
    commenter = await _make_user(db_session, "CommenterFlag3")

    r = await client.post(
        "/api/news/flag-post-3/comments",
        json={"content": "this comment contains quietword right here"},
        headers=_bearer(commenter),
    )
    assert r.status_code == 201
    comment_id = r.json()["id"]

    reports = (await db_session.execute(
        select(Report).where(Report.target_type == "comment", Report.target_id == comment_id)
    )).scalars().all()
    assert reports == []


async def test_comment_matching_keyword_case_insensitively_creates_report(client, db_session):
    author = await _make_user(db_session, "AuthorFlag4")
    await _make_news(db_session, author, slug="flag-post-4")
    await _make_rule(db_session, "BadWord")
    commenter = await _make_user(db_session, "CommenterFlag4")

    r = await client.post(
        "/api/news/flag-post-4/comments",
        json={"content": "well that is a badword right there"},
        headers=_bearer(commenter),
    )
    assert r.status_code == 201
    comment_id = r.json()["id"]

    reports = (await db_session.execute(
        select(Report).where(Report.target_type == "comment", Report.target_id == comment_id)
    )).scalars().all()
    assert len(reports) == 1
