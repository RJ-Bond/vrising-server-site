"""Regression tests for the 3-tier admin role system (moderator < admin < superadmin),
built after auditing 51 admin-gated endpoints + several manual role checks. Covers tier
boundaries, the reclassified endpoints, cross-user hierarchy protection, the one-time
migration's idempotency, the "literal string" landmine (manual checks that used to
compare against exactly "admin" and would silently reject a superadmin), and the
setup-bypass guard the migration could otherwise open."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash, role_level, ROLE_LEVELS
from backend.main import _migrate_admin_role_tiers
from backend.models import User, News, Comment, Clan, Setting

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(username=username, email=f"{username}@example.com", hashed_password=get_password_hash("x"), role=role)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


# ── 1. Tier-threshold matrix ────────────────────────────────────────────────

@pytest.mark.parametrize("role,expect_moderator,expect_admin,expect_superadmin", [
    ("user", False, False, False),
    ("moderator", True, False, False),
    ("admin", True, True, False),
    ("superadmin", True, True, True),
])
async def test_role_level_thresholds(role, expect_moderator, expect_admin, expect_superadmin):
    assert (role_level(role) >= ROLE_LEVELS["moderator"]) == expect_moderator
    assert (role_level(role) >= ROLE_LEVELS["admin"]) == expect_admin
    assert (role_level(role) >= ROLE_LEVELS["superadmin"]) == expect_superadmin


async def test_unrecognized_role_fails_closed():
    assert role_level("garbled-nonsense") == 0


@pytest.mark.parametrize("role,status_moderator_ep,status_admin_ep,status_superadmin_ep", [
    ("user", 403, 403, 403),
    ("moderator", 200, 403, 403),
    ("admin", 200, 200, 403),
    ("superadmin", 200, 200, 200),
])
async def test_endpoint_tier_matrix(client, db_session, role, status_moderator_ep, status_admin_ep, status_superadmin_ep):
    user = await _make_user(db_session, f"u_{role}", role=role)
    headers = _bearer(user)
    # moderator-tier: GET /api/admin/users ; admin-tier: GET /api/admin/stats ;
    # superadmin-tier: GET /api/admin/backups
    r_mod = await client.get("/api/admin/users", headers=headers)
    r_admin = await client.get("/api/admin/stats", headers=headers)
    r_super = await client.get("/api/admin/backups", headers=headers)
    assert r_mod.status_code == status_moderator_ep
    assert r_admin.status_code == status_admin_ep
    assert r_super.status_code == status_superadmin_ep


# ── 2. Reclassified-endpoint spot checks ────────────────────────────────────

async def test_moderator_can_list_and_delete_comments(client, db_session):
    author = await _make_user(db_session, "author1")
    news = News(title="T", slug="t-1", summary="s", content="c", author_id=author.id, published=True)
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    comment = Comment(news_id=news.id, author_id=author.id, content="hello")
    db_session.add(comment)
    await db_session.commit()
    await db_session.refresh(comment)

    mod = await _make_user(db_session, "mod1", role="moderator")
    r_list = await client.get("/api/admin/comments", headers=_bearer(mod))
    assert r_list.status_code == 200
    r_del = await client.delete(f"/api/admin/comments/{comment.id}", headers=_bearer(mod))
    assert r_del.status_code == 204


async def test_moderator_can_review_reports(client, db_session):
    from backend.models import Report
    reporter = await _make_user(db_session, "reporter1")
    rep = Report(reporter_id=reporter.id, target_type="comment", target_id=1, reason="spam")
    db_session.add(rep)
    await db_session.commit()
    await db_session.refresh(rep)

    mod = await _make_user(db_session, "mod2", role="moderator")
    r = await client.patch(f"/api/admin/reports/{rep.id}", json={"status": "reviewed", "admin_note": "ok"}, headers=_bearer(mod))
    assert r.status_code == 200


async def test_moderator_cannot_change_roles_or_reach_backups(client, db_session):
    mod = await _make_user(db_session, "mod3", role="moderator")
    victim = await _make_user(db_session, "victim1")
    r = await client.put(f"/api/admin/users/{victim.id}/role?role=admin", headers=_bearer(mod))
    assert r.status_code == 403
    r2 = await client.get("/api/admin/backup", headers=_bearer(mod))
    assert r2.status_code == 403


async def test_superadmin_can_change_role_admin_cannot(client, db_session):
    admin = await _make_user(db_session, "admin1", role="admin")
    superadmin = await _make_user(db_session, "super1", role="superadmin")
    victim = await _make_user(db_session, "victim2")

    r_admin = await client.put(f"/api/admin/users/{victim.id}/role?role=moderator", headers=_bearer(admin))
    assert r_admin.status_code == 403

    r_super = await client.put(f"/api/admin/users/{victim.id}/role?role=moderator", headers=_bearer(superadmin))
    assert r_super.status_code == 200
    await db_session.refresh(victim)
    assert victim.role == "moderator"


async def test_rcon_and_update_reject_admin_but_not_superadmin_at_auth_layer(client, db_session):
    admin = await _make_user(db_session, "admin2", role="admin")
    superadmin = await _make_user(db_session, "super2", role="superadmin")

    r_admin = await client.post("/api/admin/rcon", json={"server": 1, "command": "help"}, headers=_bearer(admin))
    assert r_admin.status_code == 403
    # Superadmin passes the auth layer; with no RCON password configured in this test DB
    # it fails with a business-logic 400, not a 403 — proves the boundary is auth, not config.
    r_super = await client.post("/api/admin/rcon", json={"server": 1, "command": "help"}, headers=_bearer(superadmin))
    assert r_super.status_code != 403


# ── 3. Hierarchy protection pairs (toggle-active / delete / bulk) ──────────

@pytest.mark.parametrize("actor_role,target_role,allowed", [
    ("moderator", "user", True),
    ("moderator", "moderator", False),
    ("moderator", "admin", False),
    ("admin", "moderator", True),
    ("admin", "admin", False),
    ("admin", "superadmin", False),
    ("superadmin", "admin", True),
    ("superadmin", "superadmin", False),
])
async def test_toggle_active_hierarchy(client, db_session, actor_role, target_role, allowed):
    actor = await _make_user(db_session, f"actor_{actor_role}_{target_role}", role=actor_role)
    target = await _make_user(db_session, f"target_{actor_role}_{target_role}", role=target_role)
    r = await client.put(f"/api/admin/users/{target.id}/toggle-active", headers=_bearer(actor))
    assert r.status_code == (200 if allowed else 403)


async def test_delete_user_hierarchy_admin_cannot_delete_admin(client, db_session):
    admin_a = await _make_user(db_session, "admin_a", role="admin")
    admin_b = await _make_user(db_session, "admin_b", role="admin")
    r = await client.delete(f"/api/admin/users/{admin_b.id}", headers=_bearer(admin_a))
    assert r.status_code == 403
    result = await db_session.execute(select(User).where(User.id == admin_b.id))
    assert result.scalar_one_or_none() is not None  # not deleted


async def test_bulk_action_excludes_peers_and_superiors(client, db_session):
    admin = await _make_user(db_session, "bulk_admin", role="admin")
    plain = await _make_user(db_session, "bulk_user", role="user")
    peer_admin = await _make_user(db_session, "bulk_peer", role="admin")
    superadmin = await _make_user(db_session, "bulk_super", role="superadmin")

    r = await client.post(
        "/api/admin/users/bulk",
        json={"user_ids": [plain.id, peer_admin.id, superadmin.id], "action": "ban"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    assert r.json()["affected"] == 1  # only the plain user
    await db_session.refresh(plain)
    await db_session.refresh(peer_admin)
    await db_session.refresh(superadmin)
    assert plain.is_active is False
    assert peer_admin.is_active is True
    assert superadmin.is_active is True


# ── 4. Self-targeting regression ────────────────────────────────────────────

async def test_self_targeting_still_blocked(client, db_session):
    admin = await _make_user(db_session, "self_admin", role="admin")
    headers = _bearer(admin)
    assert (await client.put(f"/api/admin/users/{admin.id}/toggle-active", headers=headers)).status_code == 400
    assert (await client.delete(f"/api/admin/users/{admin.id}", headers=headers)).status_code == 400
    superadmin = await _make_user(db_session, "self_super", role="superadmin")
    assert (await client.put(f"/api/admin/users/{superadmin.id}/role?role=admin", headers=_bearer(superadmin))).status_code == 400


async def test_revoke_sessions_self_allowed(client, db_session):
    admin = await _make_user(db_session, "revoke_admin", role="admin")
    assert (await client.post(f"/api/admin/users/{admin.id}/revoke-sessions", headers=_bearer(admin))).status_code == 204


async def test_revoke_sessions_peer_blocked(client, db_session):
    # Separate actor from the self-targeting test above — revoke-sessions has no
    # reissue-fresh-token step (unlike change-password), so an admin who just
    # self-revoked would otherwise fail this second call for the wrong reason
    # (their own now-revoked token), masking whether the hierarchy check fired.
    admin = await _make_user(db_session, "revoke_admin2", role="admin")
    peer = await _make_user(db_session, "revoke_peer", role="admin")
    assert (await client.post(f"/api/admin/users/{peer.id}/revoke-sessions", headers=_bearer(admin))).status_code == 403


# ── 5. Migration idempotency ────────────────────────────────────────────────

async def test_migration_promotes_admin_once_and_only_once(client, db_session, db_engine):
    import backend.database as database
    old_admin = User(username="legacy_owner", email="legacy@example.com", hashed_password=get_password_hash("x"), role="admin")
    db_session.add(old_admin)
    await db_session.commit()
    await db_session.refresh(old_admin)

    async with database.AsyncSessionLocal() as db:
        await _migrate_admin_role_tiers(db)
    await db_session.refresh(old_admin)
    assert old_admin.role == "superadmin"

    # A second run must be a no-op AND must not touch a legitimately-created new admin.
    new_admin = await _make_user(db_session, "fresh_admin", role="admin")
    async with database.AsyncSessionLocal() as db:
        await _migrate_admin_role_tiers(db)
    await db_session.refresh(new_admin)
    assert new_admin.role == "admin"


# ── 6. Literal-string-landmine regression ───────────────────────────────────

async def test_superadmin_passes_every_formerly_admin_only_manual_check(client, db_session):
    superadmin = await _make_user(db_session, "landmine_super", role="superadmin")
    headers = _bearer(superadmin)

    # Comment edit/delete on someone else's comment
    author = await _make_user(db_session, "landmine_author")
    news = News(title="T2", slug="t-2", summary="s", content="c", author_id=author.id, published=True)
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    comment = Comment(news_id=news.id, author_id=author.id, content="original")
    db_session.add(comment)
    await db_session.commit()
    await db_session.refresh(comment)
    r_edit = await client.patch(f"/api/comments/{comment.id}", json={"content": "edited by superadmin"}, headers=headers)
    assert r_edit.status_code == 200

    # Clan leader override
    clan = Clan(name="TestClan", tag="TST", leader_id=author.id)
    db_session.add(clan)
    await db_session.commit()
    await db_session.refresh(clan)
    r_clan = await client.put(f"/api/clans/{clan.id}", json={"description": "overridden"}, headers=headers)
    assert r_clan.status_code == 200

    # Maintenance status/extend
    r_maint = await client.post("/api/admin/maintenance/status", json={"text": "testing"}, headers=headers)
    assert r_maint.status_code == 200
    r_extend = await client.post("/api/admin/maintenance/extend", json={"minutes": 15}, headers=headers)
    assert r_extend.status_code in (200, 400)  # 400 only if no active maintenance window — not 403

    # Profile cosmetics
    assert (await client.put("/api/profile/admin-title", json={"title": "Owner"}, headers=headers)).status_code == 200
    assert (await client.put("/api/profile/badge-style", json={"style": "crown"}, headers=headers)).status_code == 200


async def test_moderator_also_passes_comment_and_cosmetics_checks(client, db_session):
    mod = await _make_user(db_session, "landmine_mod", role="moderator")
    headers = _bearer(mod)
    author = await _make_user(db_session, "landmine_author2")
    news = News(title="T3", slug="t-3", summary="s", content="c", author_id=author.id, published=True)
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    comment = Comment(news_id=news.id, author_id=author.id, content="original2")
    db_session.add(comment)
    await db_session.commit()
    await db_session.refresh(comment)
    assert (await client.delete(f"/api/comments/{comment.id}", headers=headers)).status_code == 204
    assert (await client.put("/api/profile/badge-style", json={"style": "flame"}, headers=headers)).status_code == 200


# ── 7. Setup-bypass guard ───────────────────────────────────────────────────

async def test_setup_bypass_guarded_after_migration(client, db_session):
    await _make_user(db_session, "post_migration_owner", role="superadmin")
    r_status = await client.get("/api/setup/status")
    assert r_status.json()["completed"] is True
    r_complete = await client.post("/api/setup/complete", json={"username": "attacker", "email": "a@a.com", "password": "password123"})
    assert r_complete.status_code == 400


# ── 8. /api/team filter ─────────────────────────────────────────────────────

async def test_team_roster_excludes_moderators(client, db_session):
    await _make_user(db_session, "team_mod", role="moderator")
    await _make_user(db_session, "team_admin", role="admin")
    await _make_user(db_session, "team_super", role="superadmin")
    r = await client.get("/api/team")
    assert r.status_code == 200
    usernames = {u["username"] for u in r.json()}
    assert "team_mod" not in usernames
    assert "team_admin" in usernames
    assert "team_super" in usernames
