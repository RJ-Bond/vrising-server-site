"""Regression test for the nav_hidden admin setting. A new Setting key needs
three separate registrations in this repo (see CLAUDE.md's "Settings are
key/value" gotcha): ALLOWED_SETTING_KEYS (save allow-list), the curated
`keys` list inside GET /api/settings/public, and admin.html's
SETTINGS_FIELD_KEYS — this covers the two backend ones so a future setting
addition that misses either one fails loudly here instead of silently 403ing
on save or being omitted from the public response in production."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_admin(db_session, username="nav_admin"):
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("x"),
        role="admin",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def test_nav_hidden_setting_accepted_and_round_trips_through_public_settings(client, db_session):
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    r = await client.put(
        "/api/admin/settings/nav_hidden",
        json={"value": '["/shop.html"]'},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["value"] == '["/shop.html"]'

    pub = await client.get("/api/settings/public")
    assert pub.status_code == 200
    assert pub.json().get("nav_hidden") == '["/shop.html"]'
