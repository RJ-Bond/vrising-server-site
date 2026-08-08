"""Regression tests for backend/routers/events.py: public event listing/detail with
per-viewer is_joined resolution, admin CRUD (create/update/delete), and the
join/leave participation flow including capacity and status guards. See the
Event/EventParticipant models in models.py."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.auth import COOKIE_NAME, create_access_token, get_password_hash
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


def _session_token(user):
    # GET /api/events resolves the optional viewer (for is_joined) straight from the
    # session cookie rather than via the get_current_user Bearer-or-cookie dependency
    # used by the auth-required endpoints — see the manual jose_jwt.decode block in
    # list_events. A Bearer Authorization header alone is invisible to it.
    return create_access_token({"sub": str(user.id)})


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


# ─── GET /api/events/mine/next ────────────────────────────────────────────────
# Powers the homepage "Продолжить" widget (frontend/index.js) — nearest upcoming/
# active event the caller is registered for.

async def test_my_next_event_requires_login(client, db_session):
    r = await client.get("/api/events/mine/next")
    assert r.status_code == 401


async def test_my_next_event_none_when_not_registered_anywhere(client, db_session):
    admin = await _make_user(db_session, "EventAdmin16", role="admin")
    user = await _make_user(db_session, "NoRegUser1", role="user")
    await _make_event(db_session, admin, title="Unjoined Event")

    r = await client.get("/api/events/mine/next", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"event": None}


async def test_my_next_event_returns_nearest_by_start_date(client, db_session):
    admin = await _make_user(db_session, "EventAdmin17", role="admin")
    user = await _make_user(db_session, "RegUser1", role="user")
    far = await _make_event(db_session, admin, title="Far Event")
    far.start_date = datetime.now(timezone.utc) + timedelta(days=10)
    near = await _make_event(db_session, admin, title="Near Event")
    near.start_date = datetime.now(timezone.utc) + timedelta(days=1)
    await db_session.commit()

    for ev in (far, near):
        r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(user))
        assert r.status_code == 200

    r = await client.get("/api/events/mine/next", headers=_bearer(user))
    assert r.status_code == 200
    body = r.json()["event"]
    assert body is not None
    assert body["title"] == "Near Event"


async def test_my_next_event_excludes_ended_and_cancelled(client, db_session):
    admin = await _make_user(db_session, "EventAdmin18", role="admin")
    user = await _make_user(db_session, "RegUser2", role="user")
    ended = await _make_event(db_session, admin, title="Ended Reg Event", status="upcoming")
    r = await client.post(f"/api/events/{ended.id}/join", headers=_bearer(user))
    assert r.status_code == 200
    ended.status = "ended"
    await db_session.commit()

    r = await client.get("/api/events/mine/next", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json() == {"event": None}


async def test_my_next_event_prioritizes_active_over_later_upcoming(client, db_session):
    """An 'active' (already-started) event the user joined should still surface ahead
    of a later 'upcoming' one — status alone gates eligibility, start_date only orders."""
    admin = await _make_user(db_session, "EventAdmin19", role="admin")
    user = await _make_user(db_session, "RegUser3", role="user")
    upcoming = await _make_event(db_session, admin, title="Later Upcoming", status="upcoming")
    upcoming.start_date = datetime.now(timezone.utc) + timedelta(days=5)
    active = await _make_event(db_session, admin, title="Ongoing Active", status="active")
    active.start_date = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    for ev in (upcoming, active):
        r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(user))
        assert r.status_code == 200

    r = await client.get("/api/events/mine/next", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["event"]["title"] == "Ongoing Active"


# ─── Batched participant-count / is_joined resolution (list_events) ─────────────
# list_events used to run one COUNT query and (for a logged-in viewer) one membership
# query PER event in the page — an N+1. It was rewritten to a single grouped-count query
# plus a single membership query for the whole page. This is a correctness regression
# test for that rewrite, not a query-count test (aiosqlite doesn't make query-counting
# practical here) — it seeds several events with different participant counts, has the
# viewer join only some of them, and asserts every event's participant_count/is_joined
# still comes out right when resolved in a batch instead of one row at a time.

async def test_list_events_batched_counts_and_is_joined_across_many_events(client, db_session):
    admin = await _make_user(db_session, "EventAdminBatch", role="admin")
    viewer = await _make_user(db_session, "ViewerBatch", role="user")
    bystanders = [await _make_user(db_session, f"Bystander{i}", role="user") for i in range(4)]

    await _make_event(db_session, admin, title="Batch Event A")  # 0 participants
    ev_b = await _make_event(db_session, admin, title="Batch Event B")  # 2 participants, viewer joined
    ev_c = await _make_event(db_session, admin, title="Batch Event C")  # 3 participants, viewer NOT joined
    ev_d = await _make_event(db_session, admin, title="Batch Event D")  # 1 participant, viewer joined

    # ev_b: viewer + 1 bystander
    db_session.add(EventParticipant(event_id=ev_b.id, user_id=viewer.id))
    db_session.add(EventParticipant(event_id=ev_b.id, user_id=bystanders[0].id))
    # ev_c: 3 bystanders, no viewer
    for b in bystanders[1:4]:
        db_session.add(EventParticipant(event_id=ev_c.id, user_id=b.id))
    # ev_d: viewer only
    db_session.add(EventParticipant(event_id=ev_d.id, user_id=viewer.id))
    await db_session.commit()

    client.cookies.set(COOKIE_NAME, _session_token(viewer))
    try:
        r = await client.get("/api/events", params={"status": "upcoming", "per_page": 50})
    finally:
        client.cookies.clear()
    assert r.status_code == 200
    by_title = {i["title"]: i for i in r.json()["items"]}

    assert by_title["Batch Event A"]["participant_count"] == 0
    assert by_title["Batch Event A"]["is_joined"] is False

    assert by_title["Batch Event B"]["participant_count"] == 2
    assert by_title["Batch Event B"]["is_joined"] is True

    assert by_title["Batch Event C"]["participant_count"] == 3
    assert by_title["Batch Event C"]["is_joined"] is False

    assert by_title["Batch Event D"]["participant_count"] == 1
    assert by_title["Batch Event D"]["is_joined"] is True


async def test_admin_event_participants_batched_lookup_multiple_users(client, db_session):
    admin = await _make_user(db_session, "EventAdminBatch2", role="admin")
    ev = await _make_event(db_session, admin, title="Multi Roster Event")
    joiners = [await _make_user(db_session, f"RosterUser{i}", role="user") for i in range(3)]

    for u in joiners:
        r = await client.post(f"/api/events/{ev.id}/join", headers=_bearer(u))
        assert r.status_code == 200

    r = await client.get(f"/api/admin/events/{ev.id}/participants", headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    got = {(row["user_id"], row["username"]) for row in body}
    expected = {(u.id, u.username) for u in joiners}
    assert got == expected
    assert all(row["registered_at"] for row in body)
