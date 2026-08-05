"""Regression tests for backend/routers/events.py: public event listing/detail with
per-viewer is_joined resolution, admin CRUD (create/update/delete), and the
join/leave participation flow including capacity and status guards. See the
Event/EventParticipant models in models.py."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import Event, EventParticipant, User

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


async def _make_event(db_session, creator, title="Blood Moon Tournament", status="upcoming",
                       max_participants=None, event_type="pvp"):
    ev = Event(
        title=title,
        description="desc",
        event_type=event_type,
        start_date=datetime.now(timezone.utc) + timedelta(days=1),
        max_participants=max_participants,
        status=status,
        created_by=creator.id,
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)
    return ev


# ─── GET /api/events (list) ─────────────────────────────────────────────────

async def test_list_events_filters_by_status_and_includes_participant_count(client, db_session):
    admin = await _make_user(db_session, "EventAdmin1", role="admin")
    await _make_event(db_session, admin, title="Upcoming Event", status="upcoming")
    await _make_event(db_session, admin, title="Ended Event", status="ended")

    r = await client.get("/api/events", params={"status": "upcoming"})
    assert r.status_code == 200
    body = r.json()
    titles = [i["title"] for i in body["items"]]
    assert "Upcoming Event" in titles
    assert "Ended Event" not in titles
    assert body["total"] == 1


async def test_list_events_anonymous_is_joined_false(client, db_session):
    admin = await _make_user(db_session, "EventAdmin2", role="admin")
    await _make_event(db_session, admin, title="Anon Check Event")

    r = await client.get("/api/events", params={"status": "upcoming"})
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["is_joined"] is False
    assert item["participant_count"] == 0


# ─── GET /api/events/{id} ────────────────────────────────────────────────────

async def test_get_event_detail_404_for_missing(client, db_session):
    r = await client.get("/api/events/99999")
    assert r.status_code == 404


async def test_get_event_detail_returns_expected_shape(client, db_session):
    admin = await _make_user(db_session, "EventAdmin3", role="admin")
    ev = await _make_event(db_session, admin, title="Detail Event")

    r = await client.get(f"/api/events/{ev.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == ev.id
    assert body["title"] == "Detail Event"
    assert body["participant_count"] == 0
    assert body["is_joined"] is False


# ─── POST /api/admin/events (admin-only create) ─────────────────────────────

async def test_admin_create_event_requires_admin(client, db_session):
    user = await _make_user(db_session, "PlainUser1", role="user")
    r = await client.post(
        "/api/admin/events",
        json={"title": "Sneaky Event", "start_date": "2027-01-01T00:00:00"},
        headers=_bearer(user),
    )
    assert r.status_code == 403


async def test_admin_create_event_happy_path(client, db_session):
    admin = await _make_user(db_session, "EventAdmin4", role="admin")
    r = await client.post(
        "/api/admin/events",
        json={
            "title": "New Tournament",
            "description": "Fight!",
            "event_type": "pvp",
            "start_date": "2027-01-01T00:00:00",
            "max_participants": 10,
        },
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "New Tournament"
    assert body["status"] == "upcoming"

    row = (await db_session.execute(select(Event).where(Event.id == body["id"]))).scalar_one()
    assert row.created_by == admin.id


# ─── PUT /api/admin/events/{id} ──────────────────────────────────────────────

async def test_admin_update_event_changes_fields_and_status(client, db_session):
    admin = await _make_user(db_session, "EventAdmin5", role="admin")
    ev = await _make_event(db_session, admin, title="Old Title")

    r = await client.put(
        f"/api/admin/events/{ev.id}",
        json={
            "title": "New Title",
            "start_date": "2027-02-01T00:00:00",
            "status": "active",
        },
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "New Title"
    assert body["status"] == "active"


async def test_admin_update_event_404_for_missing(client, db_session):
    admin = await _make_user(db_session, "EventAdmin6", role="admin")
    r = await client.put(
        "/api/admin/events/99999",
        json={"title": "X", "start_date": "2027-01-01T00:00:00"},
        headers=_bearer(admin),
    )
    assert r.status_code == 404


# ─── DELETE /api/admin/events/{id} ───────────────────────────────────────────

async def test_admin_delete_event_removes_it_and_participants(client, db_session):
    admin = await _make_user(db_session, "EventAdmin7", role="admin")
    joiner = await _make_user(db_session, "Joiner1", role="user")
    ev = await _make_event(db_session, admin, title="Doomed Event")

    r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(joiner))
    assert r.status_code == 200

    r2 = await client.delete(f"/api/admin/events/{ev.id}", headers=_bearer(admin))
    assert r2.status_code == 204

    remaining = (await db_session.execute(select(Event).where(Event.id == ev.id))).scalar_one_or_none()
    assert remaining is None
    parts = (await db_session.execute(
        select(EventParticipant).where(EventParticipant.event_id == ev.id)
    )).scalars().all()
    assert parts == []


async def test_admin_delete_event_requires_admin(client, db_session):
    user = await _make_user(db_session, "PlainUser2", role="user")
    admin = await _make_user(db_session, "EventAdmin8", role="admin")
    ev = await _make_event(db_session, admin, title="Protected Event")

    r = await client.delete(f"/api/admin/events/{ev.id}", headers=_bearer(user))
    assert r.status_code == 403


# ─── POST /api/events/{id}/join & DELETE .../leave ───────────────────────────

async def test_join_event_happy_path_and_duplicate_rejected(client, db_session):
    admin = await _make_user(db_session, "EventAdmin9", role="admin")
    user = await _make_user(db_session, "Joiner2", role="user")
    ev = await _make_event(db_session, admin, title="Joinable Event")

    r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r2 = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(user))
    assert r2.status_code == 400


async def test_join_event_full_capacity_rejected(client, db_session):
    admin = await _make_user(db_session, "EventAdmin10", role="admin")
    ev = await _make_event(db_session, admin, title="Tiny Event", max_participants=1)
    u1 = await _make_user(db_session, "Joiner3", role="user")
    u2 = await _make_user(db_session, "Joiner4", role="user")

    r1 = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(u1))
    assert r1.status_code == 200
    r2 = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(u2))
    assert r2.status_code == 400
    assert "full" in r2.json()["detail"].lower()


async def test_join_ended_event_rejected(client, db_session):
    admin = await _make_user(db_session, "EventAdmin11", role="admin")
    user = await _make_user(db_session, "Joiner5", role="user")
    ev = await _make_event(db_session, admin, title="Ended Event 2", status="ended")

    r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(user))
    assert r.status_code == 400


async def test_join_event_requires_login(client, db_session):
    admin = await _make_user(db_session, "EventAdmin12", role="admin")
    ev = await _make_event(db_session, admin, title="Login Required Event")
    r = await client.post(f"/api/events/{ev.id}/join")
    assert r.status_code == 401


async def test_leave_event_happy_path_and_not_participant_404s(client, db_session):
    admin = await _make_user(db_session, "EventAdmin13", role="admin")
    user = await _make_user(db_session, "Joiner6", role="user")
    ev = await _make_event(db_session, admin, title="Leavable Event")

    r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(user))
    assert r.status_code == 200

    r2 = await client.delete(f"/api/events/{ev.id}/leave", headers=_bearer(user))
    assert r2.status_code == 204

    r3 = await client.delete(f"/api/events/{ev.id}/leave", headers=_bearer(user))
    assert r3.status_code == 404


# ─── GET /api/admin/events/{id}/participants ─────────────────────────────────

async def test_admin_event_participants_lists_joined_users(client, db_session):
    admin = await _make_user(db_session, "EventAdmin14", role="admin")
    user = await _make_user(db_session, "Joiner7", role="user")
    ev = await _make_event(db_session, admin, title="Roster Event")

    r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(user))
    assert r.status_code == 200

    r2 = await client.get(f"/api/admin/events/{ev.id}/participants", headers=_bearer(admin))
    assert r2.status_code == 200
    body = r2.json()
    assert len(body) == 1
    assert body[0]["username"] == "Joiner7"
    assert body[0]["user_id"] == user.id


async def test_admin_event_participants_requires_admin(client, db_session):
    admin = await _make_user(db_session, "EventAdmin15", role="admin")
    user = await _make_user(db_session, "PlainUser3", role="user")
    ev = await _make_event(db_session, admin, title="Protected Roster Event")

    r = await client.get(f"/api/admin/events/{ev.id}/participants", headers=_bearer(user))
    assert r.status_code == 403
