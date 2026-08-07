"""Coverage for backend/routers/profile.py — previously essentially untested (the
similarly-named test_nickname_change.py covers the unrelated plugin ".nick" chat
command, not this router). One happy-path + one auth/permission-boundary case per
endpoint, plus the export endpoint's cross-user data isolation."""
import base64
from io import BytesIO

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import Comment, News, PointsTransaction, ShopRedemption, User

pytestmark = pytest.mark.asyncio

# A minimal valid 1x1 transparent PNG, used for the raster-upload endpoints.
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def _make_user(db_session, username, role="user", email=None, points_balance=0):
    user = User(
        username=username,
        email=email or f"{username.lower()}@example.com",
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


def _png_file(name="pic.png"):
    return {"file": (name, BytesIO(_PNG_1PX), "image/png")}


# ── PUT /api/profile/bio ────────────────────────────────────────────────────

async def test_update_bio_happy_path(client, db_session):
    user = await _make_user(db_session, "BioUser")
    r = await client.put("/api/profile/bio", json={"bio": "Hello world"}, headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["bio"] == "Hello world"
    await db_session.refresh(user)
    assert user.bio == "Hello world"


async def test_update_bio_requires_auth(client, db_session):
    r = await client.put("/api/profile/bio", json={"bio": "Hello"})
    assert r.status_code == 401


# ── PUT /api/profile/game-nickname ──────────────────────────────────────────

async def test_update_game_nickname_happy_path(client, db_session):
    user = await _make_user(db_session, "NickUser")
    r = await client.put("/api/profile/game-nickname", json={"game_nickname": "CoolPlayer"}, headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["game_nickname"] == "CoolPlayer"
    await db_session.refresh(user)
    assert user.game_nickname == "CoolPlayer"


async def test_update_game_nickname_requires_auth(client, db_session):
    r = await client.put("/api/profile/game-nickname", json={"game_nickname": "X"})
    assert r.status_code == 401


# ── GET /api/team ────────────────────────────────────────────────────────────

async def test_get_team_lists_admins_but_not_moderators_or_plain_users(client, db_session):
    await _make_user(db_session, "TeamPlain", role="user")
    await _make_user(db_session, "TeamMod", role="moderator")
    admin = await _make_user(db_session, "TeamAdmin", role="admin")
    superadmin = await _make_user(db_session, "TeamSuper", role="superadmin")

    r = await client.get("/api/team")
    assert r.status_code == 200
    names = {row["username"] for row in r.json()}
    assert names == {admin.username, superadmin.username}


# ── PUT /api/profile/admin-title ────────────────────────────────────────────

async def test_set_admin_title_happy_path_as_moderator(client, db_session):
    mod = await _make_user(db_session, "TitleMod", role="moderator")
    r = await client.put("/api/profile/admin-title", json={"title": "Chief Moderator"}, headers=_bearer(mod))
    assert r.status_code == 200
    assert r.json()["admin_title"] == "Chief Moderator"


async def test_set_admin_title_rejects_plain_user(client, db_session):
    user = await _make_user(db_session, "TitlePlain", role="user")
    r = await client.put("/api/profile/admin-title", json={"title": "Boss"}, headers=_bearer(user))
    assert r.status_code == 403


# ── POST /api/profile/badge-icon ────────────────────────────────────────────

async def test_upload_badge_icon_happy_path_as_moderator(client, db_session):
    mod = await _make_user(db_session, "BadgeIconMod", role="moderator")
    r = await client.post("/api/profile/badge-icon", files=_png_file(), headers=_bearer(mod))
    assert r.status_code == 200
    assert r.json()["badge_icon_url"].startswith("/api/uploads/badge_")


async def test_upload_badge_icon_rejects_plain_user(client, db_session):
    user = await _make_user(db_session, "BadgeIconPlain", role="user")
    r = await client.post("/api/profile/badge-icon", files=_png_file(), headers=_bearer(user))
    assert r.status_code == 403


# ── DELETE /api/profile/badge-icon ──────────────────────────────────────────

async def test_clear_badge_icon_happy_path_as_moderator(client, db_session):
    mod = await _make_user(db_session, "BadgeClearMod", role="moderator")
    up = await client.post("/api/profile/badge-icon", files=_png_file(), headers=_bearer(mod))
    assert up.status_code == 200

    r = await client.delete("/api/profile/badge-icon", headers=_bearer(mod))
    assert r.status_code == 204

    result = await db_session.execute(select(User).where(User.id == mod.id))
    refreshed = result.scalar_one()
    assert refreshed.badge_icon_url is None


async def test_clear_badge_icon_rejects_plain_user(client, db_session):
    user = await _make_user(db_session, "BadgeClearPlain", role="user")
    r = await client.delete("/api/profile/badge-icon", headers=_bearer(user))
    assert r.status_code == 403


# ── PUT /api/profile/badge-style ────────────────────────────────────────────

async def test_set_badge_style_happy_path_as_moderator(client, db_session):
    mod = await _make_user(db_session, "BadgeStyleMod", role="moderator")
    r = await client.put("/api/profile/badge-style", json={"style": "crown"}, headers=_bearer(mod))
    assert r.status_code == 200
    assert r.json()["badge_style"] == "crown"


async def test_set_badge_style_rejects_plain_user(client, db_session):
    user = await _make_user(db_session, "BadgeStylePlain", role="user")
    r = await client.put("/api/profile/badge-style", json={"style": "crown"}, headers=_bearer(user))
    assert r.status_code == 403


async def test_set_badge_style_rejects_invalid_style(client, db_session):
    mod = await _make_user(db_session, "BadgeStyleInvalid", role="moderator")
    r = await client.put("/api/profile/badge-style", json={"style": "not-a-real-style"}, headers=_bearer(mod))
    assert r.status_code == 400


# ── PUT /api/profile/newsletter ─────────────────────────────────────────────

async def test_set_newsletter_opt_in_happy_path(client, db_session):
    user = await _make_user(db_session, "NewsletterUser")
    r = await client.put("/api/profile/newsletter", json={"newsletter_opt_in": True}, headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["newsletter_opt_in"] is True
    await db_session.refresh(user)
    assert user.newsletter_opt_in is True


async def test_set_newsletter_opt_in_requires_auth(client, db_session):
    r = await client.put("/api/profile/newsletter", json={"newsletter_opt_in": True})
    assert r.status_code == 401


# ── POST /api/profile/cover ─────────────────────────────────────────────────

async def test_upload_cover_happy_path(client, db_session):
    user = await _make_user(db_session, "CoverUser")
    r = await client.post("/api/profile/cover", files=_png_file(), headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["cover_url"].startswith("/api/uploads/covers/cover_")
    await db_session.refresh(user)
    assert user.cover_url == r.json()["cover_url"]


async def test_upload_cover_rejects_non_image_content_type(client, db_session):
    user = await _make_user(db_session, "CoverBadType")
    r = await client.post(
        "/api/profile/cover",
        files={"file": ("notes.txt", BytesIO(b"just text"), "text/plain")},
        headers=_bearer(user),
    )
    assert r.status_code == 400


# ── GET /api/profile/export ─────────────────────────────────────────────────

async def test_export_requires_auth(client, db_session):
    r = await client.get("/api/profile/export")
    assert r.status_code == 401


async def test_export_happy_path_has_download_headers_and_own_data(client, db_session):
    user = await _make_user(db_session, "ExportUser", points_balance=250)
    db_session.add(PointsTransaction(user_id=user.id, delta=250, balance_after=250, reason="donation"))
    db_session.add(ShopRedemption(
        user_id=user.id, item_name_snapshot="VIP Kit", cost_snapshot=100, status="pending",
    ))
    news = News(title="Patch notes", slug="patch-notes-export-test", summary="s", content="c", author_id=user.id)
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    db_session.add(Comment(news_id=news.id, author_id=user.id, content="My own comment"))
    await db_session.commit()

    r = await client.get("/api/profile/export", headers=_bearer(user))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"] == 'attachment; filename="my-data.json"'

    body = r.json()
    assert body["account"]["username"] == "ExportUser"
    assert len(body["points_transactions"]) == 1
    assert body["points_transactions"][0]["reason"] == "donation"
    assert len(body["shop_redemptions"]) == 1
    assert body["shop_redemptions"][0]["item_name"] == "VIP Kit"
    assert len(body["comments"]) == 1
    assert body["comments"][0]["content"] == "My own comment"


async def test_export_never_contains_another_users_data(client, db_session):
    user_a = await _make_user(db_session, "ExportAlice")
    user_b = await _make_user(db_session, "ExportBob")

    db_session.add(PointsTransaction(user_id=user_a.id, delta=10, balance_after=10, reason="playtime"))
    db_session.add(PointsTransaction(user_id=user_b.id, delta=999, balance_after=999, reason="donation", detail="bob-only"))
    db_session.add(ShopRedemption(user_id=user_b.id, item_name_snapshot="Bob's Item", cost_snapshot=50, status="pending"))

    news = News(title="Shared post", slug="patch-notes-export-isolation", summary="s", content="c", author_id=user_a.id)
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    db_session.add(Comment(news_id=news.id, author_id=user_b.id, content="Bob's comment"))
    await db_session.commit()

    r = await client.get("/api/profile/export", headers=_bearer(user_a))
    assert r.status_code == 200
    body = r.json()

    assert body["account"]["username"] == "ExportAlice"
    assert len(body["points_transactions"]) == 1
    assert body["points_transactions"][0]["reason"] == "playtime"
    assert all(tx["detail"] != "bob-only" for tx in body["points_transactions"])
    assert body["shop_redemptions"] == []
    assert body["comments"] == []
