"""Regression tests for backend/routers/wipes.py: public listing of server wipe
history (ordered newest-first) and admin-only create/delete. See the Wipe model
in models.py and WipeCreate/WipeOut in schemas.py."""
from datetime import datetime

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import User, Wipe

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


async def _make_wipe(db_session, wipe_date, server_num=1, wipe_type="full", note=None):
    w = Wipe(server_num=server_num, wipe_type=wipe_type, wipe_date=wipe_date, note=note)
    db_session.add(w)
    await db_session.commit()
    await db_session.refresh(w)
    return w


# ─── GET /api/wipes ───────────────────────────────────────────────────────────

async def test_list_wipes_is_public_and_ordered_newest_first(client, db_session):
    older = await _make_wipe(db_session, datetime(2025, 1, 1))
    newer = await _make_wipe(db_session, datetime(2025, 6, 1))

    r = await client.get("/api/wipes")
    assert r.status_code == 200
    body = r.json()
    ids = [w["id"] for w in body]
    assert ids.index(newer.id) < ids.index(older.id)


async def test_list_wipes_empty(client, db_session):
    r = await client.get("/api/wipes")
    assert r.status_code == 200
    assert r.json() == []


# ─── POST /api/admin/wipes ────────────────────────────────────────────────────

async def test_create_wipe_requires_admin(client, db_session):
    user = await _make_user(db_session, "WipeUser1", role="user")
    r = await client.post(
        "/api/admin/wipes",
        json={"server_num": 1, "wipe_type": "full", "wipe_date": "2026-01-01T00:00:00"},
        headers=_bearer(user),
    )
    assert r.status_code == 403


async def test_create_wipe_happy_path(client, db_session):
    admin = await _make_user(db_session, "WipeAdmin1", role="admin")
    r = await client.post(
        "/api/admin/wipes",
        json={"server_num": 2, "wipe_type": "map", "wipe_date": "2026-03-01T00:00:00", "note": "Season 3"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["server_num"] == 2
    assert body["wipe_type"] == "map"
    assert body["note"] == "Season 3"

    row = (await db_session.execute(select(Wipe).where(Wipe.id == body["id"]))).scalar_one()
    assert row.wipe_type == "map"


async def test_create_wipe_rejects_invalid_wipe_type(client, db_session):
    admin = await _make_user(db_session, "WipeAdmin2", role="admin")
    r = await client.post(
        "/api/admin/wipes",
        json={"server_num": 1, "wipe_type": "bogus", "wipe_date": "2026-01-01T00:00:00"},
        headers=_bearer(admin),
    )
    assert r.status_code == 422


# ─── DELETE /api/admin/wipes/{id} ────────────────────────────────────────────

async def test_delete_wipe_requires_admin(client, db_session):
    user = await _make_user(db_session, "WipeUser2", role="user")
    w = await _make_wipe(db_session, datetime(2025, 5, 1))
    r = await client.delete(f"/api/admin/wipes/{w.id}", headers=_bearer(user))
    assert r.status_code == 403

    still_there = await db_session.get(Wipe, w.id)
    assert still_there is not None


async def test_delete_wipe_happy_path(client, db_session):
    admin = await _make_user(db_session, "WipeAdmin3", role="admin")
    w = await _make_wipe(db_session, datetime(2025, 5, 1))

    r = await client.delete(f"/api/admin/wipes/{w.id}", headers=_bearer(admin))
    assert r.status_code == 204

    # `w` is already in db_session's identity map (loaded by _make_wipe above) —
    # db_session.get() would return that cached instance without re-querying. Force a
    # real reload via select() to see what the request's own session actually committed.
    remaining = (await db_session.execute(select(Wipe).where(Wipe.id == w.id))).scalar_one_or_none()
    assert remaining is None


async def test_delete_wipe_404_for_missing(client, db_session):
    admin = await _make_user(db_session, "WipeAdmin4", role="admin")
    r = await client.delete("/api/admin/wipes/99999", headers=_bearer(admin))
    assert r.status_code == 404
