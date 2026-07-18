"""Regression tests for the admin-facing linked-game-accounts view:
GET /api/admin/linked-accounts and POST /api/admin/users/{id}/unlink-steam — the
site accounts linked to a SteamID via the in-game .register/.login flow (User.steam_id)."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_admin(db_session, username="AdminUser"):
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


async def _make_linked_user(db_session, username, steam_id):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id=steam_id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ─── Auth gating ────────────────────────────────────────────────────────────

async def test_list_linked_accounts_requires_admin_auth(client, db_session):
    r = await client.get("/api/admin/linked-accounts")
    assert r.status_code == 401


async def test_unlink_steam_requires_admin_auth(client, db_session):
    r = await client.post("/api/admin/users/1/unlink-steam")
    assert r.status_code == 401


# ─── GET /api/admin/linked-accounts ────────────────────────────────────────

async def test_list_linked_accounts_returns_only_users_with_steam_id(client, db_session):
    admin = await _make_admin(db_session)
    linked = await _make_linked_user(db_session, "LinkedPlayer", "76561198000000001")
    db_session.add(User(
        username="UnlinkedPlayer",
        email="unlinkedplayer@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
    ))
    await db_session.commit()

    r = await client.get("/api/admin/linked-accounts", headers=_bearer(admin))
    assert r.status_code == 200
    body = r.json()
    usernames = [row["username"] for row in body]
    assert "LinkedPlayer" in usernames
    assert "UnlinkedPlayer" not in usernames
    assert admin.username not in usernames  # admin itself has no steam_id


async def test_list_linked_accounts_returns_expected_fields(client, db_session):
    admin = await _make_admin(db_session)
    linked = await _make_linked_user(db_session, "FieldCheck", "76561198000000002")

    r = await client.get("/api/admin/linked-accounts", headers=_bearer(admin))
    assert r.status_code == 200
    row = next(row for row in r.json() if row["username"] == "FieldCheck")
    assert row["id"] == linked.id
    assert row["steam_id"] == "76561198000000002"
    assert "created_at" in row
    assert "avatar_url" in row
    assert "email" not in row
    assert "role" not in row


# ─── POST /api/admin/users/{id}/unlink-steam ───────────────────────────────

async def test_unlink_steam_clears_steam_id(client, db_session):
    admin = await _make_admin(db_session)
    linked = await _make_linked_user(db_session, "ToUnlink", "76561198000000003")

    r = await client.post(f"/api/admin/users/{linked.id}/unlink-steam", headers=_bearer(admin))
    assert r.status_code == 200

    await db_session.refresh(linked)
    assert linked.steam_id is None


async def test_unlink_steam_removes_user_from_linked_accounts_list(client, db_session):
    admin = await _make_admin(db_session)
    linked = await _make_linked_user(db_session, "GoneAfterUnlink", "76561198000000004")

    r = await client.post(f"/api/admin/users/{linked.id}/unlink-steam", headers=_bearer(admin))
    assert r.status_code == 200

    list_r = await client.get("/api/admin/linked-accounts", headers=_bearer(admin))
    assert list_r.status_code == 200
    usernames = [row["username"] for row in list_r.json()]
    assert "GoneAfterUnlink" not in usernames


async def test_unlink_steam_404_for_unknown_user(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.post("/api/admin/users/999999/unlink-steam", headers=_bearer(admin))
    assert r.status_code == 404
