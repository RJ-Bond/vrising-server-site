"""Regression tests for the ban-appeals system:
- POST /api/appeals (public, no auth/plugin-key — a banned player has neither)
- GET /api/admin/appeals, POST /api/admin/appeals/{id}/resolve (admin-JWT gated)

See models.BanAppeal's docstring for the full lifecycle: an appeal is submitted against
whatever active Ban currently exists for a steam_id, and approving it lifts that ban the
same way the existing bans.html "Разбанить" button does (Ban.unban_at -> now)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import Ban, BanAppeal, User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """POST /api/appeals is rate-limited (3/hour per IP) to keep the endpoint
    unauthenticated-write-safe; several tests below submit more than 3 appeals total, so
    reset slowapi's in-memory counters before each test rather than sharing state across
    the whole module (or the whole test session, since `app` is a process-wide
    singleton)."""
    from backend.main import app
    app.state.limiter.reset()
    yield


async def _make_admin(db_session, username="AppealsAdmin"):
    admin = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("adminpass1"),
        role="admin",
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    return admin


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _make_active_ban(db_session, steam_id="steam-appeal-1", **kwargs):
    ban = Ban(
        server_num=kwargs.pop("server_num", 1),
        steam_id=steam_id,
        character_name=kwargs.pop("character_name", "Appealer"),
        admin_name=kwargs.pop("admin_name", "Overseer"),
        reason=kwargs.pop("reason", "duping"),
        banned_at=datetime.utcnow(),
        unban_at=kwargs.pop("unban_at", None),
    )
    db_session.add(ban)
    await db_session.commit()
    await db_session.refresh(ban)
    return ban


# ─── POST /api/appeals ───────────────────────────────────────────────────────

async def test_submit_appeal_without_active_ban_rejected(client, db_session):
    r = await client.post(
        "/api/appeals",
        json={"steam_id": "no-such-ban", "character_name": "Nobody", "message": "please unban me"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "no_active_ban"


async def test_submit_appeal_with_active_ban_succeeds(client, db_session):
    ban = await _make_active_ban(db_session)

    r = await client.post(
        "/api/appeals",
        json={"steam_id": ban.steam_id, "character_name": "IgnoredName", "message": "It was a mistake"},
    )
    assert r.status_code == 200
    assert r.json() == {"success": True}

    result = await db_session.execute(
        select(BanAppeal).where(BanAppeal.steam_id == ban.steam_id)
    )
    appeal = result.scalar_one()
    assert appeal.ban_id == ban.id
    assert appeal.status == "pending"
    # character_name is authoritative from the Ban row, not the submitted body
    assert appeal.character_name == ban.character_name
    assert appeal.message == "It was a mistake"


async def test_submit_second_appeal_for_same_pending_ban_rejected(client, db_session):
    ban = await _make_active_ban(db_session, steam_id="steam-appeal-2")

    r1 = await client.post(
        "/api/appeals",
        json={"steam_id": ban.steam_id, "character_name": "X", "message": "first appeal"},
    )
    assert r1.status_code == 200

    r2 = await client.post(
        "/api/appeals",
        json={"steam_id": ban.steam_id, "character_name": "X", "message": "second appeal"},
    )
    assert r2.status_code == 400
    assert r2.json()["detail"] == "already_appealed"


# ─── GET /api/admin/appeals ──────────────────────────────────────────────────

async def test_admin_appeals_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/appeals")
    assert r.status_code == 401


async def test_admin_appeals_lists_with_ban_context(client, db_session):
    ban = await _make_active_ban(db_session, steam_id="steam-appeal-3", reason="griefing", admin_name="ModX")
    await client.post(
        "/api/appeals",
        json={"steam_id": ban.steam_id, "character_name": "X", "message": "wasn't me"},
    )

    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/appeals", headers=_bearer(admin))
    assert r.status_code == 200
    appeals = r.json()["appeals"]
    assert len(appeals) == 1
    a = appeals[0]
    assert a["steam_id"] == "steam-appeal-3"
    assert a["status"] == "pending"
    assert a["ban_reason"] == "griefing"
    assert a["ban_admin_name"] == "ModX"


async def test_admin_appeals_status_filter(client, db_session):
    ban1 = await _make_active_ban(db_session, steam_id="steam-appeal-4")
    ban2 = await _make_active_ban(db_session, steam_id="steam-appeal-5")
    await client.post("/api/appeals", json={"steam_id": ban1.steam_id, "character_name": "X", "message": "m1"})
    await client.post("/api/appeals", json={"steam_id": ban2.steam_id, "character_name": "X", "message": "m2"})

    admin = await _make_admin(db_session)
    # Resolve one of them so we have one pending + one approved.
    list_r = await client.get("/api/admin/appeals", headers=_bearer(admin))
    appeal_to_resolve = next(a for a in list_r.json()["appeals"] if a["steam_id"] == "steam-appeal-4")
    await client.post(
        f"/api/admin/appeals/{appeal_to_resolve['id']}/resolve",
        json={"approve": True, "admin_response": "ok"},
        headers=_bearer(admin),
    )

    pending_r = await client.get("/api/admin/appeals", params={"status": "pending"}, headers=_bearer(admin))
    pending_steam_ids = [a["steam_id"] for a in pending_r.json()["appeals"]]
    assert pending_steam_ids == ["steam-appeal-5"]

    approved_r = await client.get("/api/admin/appeals", params={"status": "approved"}, headers=_bearer(admin))
    approved_steam_ids = [a["steam_id"] for a in approved_r.json()["appeals"]]
    assert approved_steam_ids == ["steam-appeal-4"]


# ─── POST /api/admin/appeals/{id}/resolve ────────────────────────────────────

async def test_resolve_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/appeals/1/resolve", json={"approve": True, "admin_response": "ok"})
    assert r.status_code == 401


async def test_resolve_approve_lifts_ban_and_marks_appeal(client, db_session):
    ban = await _make_active_ban(db_session, steam_id="steam-appeal-6", unban_at=datetime.utcnow() + timedelta(days=5))
    await client.post("/api/appeals", json={"steam_id": ban.steam_id, "character_name": "X", "message": "m"})

    admin = await _make_admin(db_session)
    list_r = await client.get("/api/admin/appeals", headers=_bearer(admin))
    appeal_id = list_r.json()["appeals"][0]["id"]

    r = await client.post(
        f"/api/admin/appeals/{appeal_id}/resolve",
        json={"approve": True, "admin_response": "Approved, welcome back"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    assert r.json() == {"success": True}

    # Appeal marked approved/resolved
    after_r = await client.get("/api/admin/appeals", headers=_bearer(admin))
    resolved = next(a for a in after_r.json()["appeals"] if a["id"] == appeal_id)
    assert resolved["status"] == "approved"
    assert resolved["admin_response"] == "Approved, welcome back"
    assert resolved["admin_name"] == admin.username
    assert resolved["resolved_at"] is not None

    # Underlying ban's unban_at was brought forward to ~now (not unbanned_at directly)
    await db_session.refresh(ban)
    assert ban.unbanned_at is None
    now = datetime.now(timezone.utc)
    unban_at = ban.unban_at.replace(tzinfo=timezone.utc)
    assert abs((unban_at - now).total_seconds()) < 10


async def test_resolve_reject_does_not_touch_ban(client, db_session):
    original_unban_at = datetime.utcnow() + timedelta(days=5)
    ban = await _make_active_ban(db_session, steam_id="steam-appeal-7", unban_at=original_unban_at)
    await client.post("/api/appeals", json={"steam_id": ban.steam_id, "character_name": "X", "message": "m"})

    admin = await _make_admin(db_session)
    list_r = await client.get("/api/admin/appeals", headers=_bearer(admin))
    appeal_id = list_r.json()["appeals"][0]["id"]

    r = await client.post(
        f"/api/admin/appeals/{appeal_id}/resolve",
        json={"approve": False, "admin_response": "Denied"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200

    after_r = await client.get("/api/admin/appeals", headers=_bearer(admin))
    resolved = next(a for a in after_r.json()["appeals"] if a["id"] == appeal_id)
    assert resolved["status"] == "rejected"

    await db_session.refresh(ban)
    assert ban.unban_at == original_unban_at


async def test_resolve_404_for_nonexistent_appeal(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post(
        "/api/admin/appeals/999999/resolve",
        json={"approve": True, "admin_response": "ok"},
        headers=_bearer(admin),
    )
    assert r.status_code == 404


async def test_resolve_404_for_already_resolved_appeal(client, db_session):
    ban = await _make_active_ban(db_session, steam_id="steam-appeal-8")
    await client.post("/api/appeals", json={"steam_id": ban.steam_id, "character_name": "X", "message": "m"})

    admin = await _make_admin(db_session)
    list_r = await client.get("/api/admin/appeals", headers=_bearer(admin))
    appeal_id = list_r.json()["appeals"][0]["id"]

    first = await client.post(
        f"/api/admin/appeals/{appeal_id}/resolve",
        json={"approve": True, "admin_response": "ok"},
        headers=_bearer(admin),
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/admin/appeals/{appeal_id}/resolve",
        json={"approve": True, "admin_response": "ok again"},
        headers=_bearer(admin),
    )
    assert second.status_code == 404
