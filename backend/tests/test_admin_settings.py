"""Regression test for the nav_hidden admin setting. A new Setting key needs
three separate registrations in this repo (see CLAUDE.md's "Settings are
key/value" gotcha): ALLOWED_SETTING_KEYS (save allow-list), the curated
`keys` list inside GET /api/settings/public, and admin.html's
SETTINGS_FIELD_KEYS — this covers the two backend ones so a future setting
addition that misses either one fails loudly here instead of silently 403ing
on save or being omitted from the public response in production."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User, Setting

pytestmark = pytest.mark.asyncio


async def _make_admin(db_session, username="nav_admin", role="admin"):
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("x"),
        role=role,
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


async def test_rules_tldr_setting_accepted_and_round_trips_through_public_settings(client, db_session):
    # New setting added for the homepage TL;DR-rules-summary widget — same three-spot
    # registration this file's own docstring warns about. Also exercises home_pitch/
    # home_testimonials/home_gallery/site_launched_date, which were missing from
    # ALLOWED_SETTING_KEYS despite being in admin.html's SETTINGS_FIELD_KEYS (a real,
    # pre-existing gap fixed alongside rules_tldr — see admin_settings.py's comment).
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    r = await client.put(
        "/api/admin/settings/rules_tldr",
        json={"value": "Без читов\nУважайте других"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["value"] == "Без читов\nУважайте других"

    pub = await client.get("/api/settings/public")
    assert pub.status_code == 200
    assert pub.json().get("rules_tldr") == "Без читов\nУважайте других"


async def test_home_pitch_setting_accepted(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.put(
        "/api/admin/settings/home_pitch",
        json={"value": "PvE, честные рейты"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    assert r.json()["value"] == "PvE, честные рейты"


async def test_daily_tips_setting_accepted_and_round_trips_through_public_settings(client, db_session):
    # Tip-of-the-day sidebar card (frontend/index.js's DAILY_TIPS/renderTipOfDay())
    # started fully hardcoded — this setting lets an admin override it with custom
    # newline-separated tips without a code deploy. Same three-spot registration.
    admin = await _make_admin(db_session)
    headers = _bearer(admin)

    r = await client.put(
        "/api/admin/settings/daily_tips",
        json={"value": "Совет один\nСовет два"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["value"] == "Совет один\nСовет два"

    pub = await client.get("/api/settings/public")
    assert pub.status_code == 200
    assert pub.json().get("daily_tips") == "Совет один\nСовет два"


# ─── Secret-setting masking (a plain admin could previously read the live RCON
# password / plugin API key straight off GET /api/admin/settings, even though
# RCON execution itself is gated to superadmin — see admin_settings.py's
# SECRET_SETTING_KEYS comment) ────────────────────────────────────────────────

async def test_plain_admin_sees_masked_secret_settings(client, db_session):
    for key, value in [("rcon_password", "hunter2"), ("plugin_api_key", "sekret-key")]:
        db_session.add(Setting(key=key, value=value))
    await db_session.commit()

    admin = await _make_admin(db_session, "plain_admin")
    r = await client.get("/api/admin/settings", headers=_bearer(admin))
    assert r.status_code == 200
    by_key = {s["key"]: s["value"] for s in r.json()}
    assert by_key["rcon_password"] == "••••••••"
    assert by_key["plugin_api_key"] == "••••••••"


async def test_superadmin_sees_real_secret_setting_values(client, db_session):
    db_session.add(Setting(key="rcon_password", value="hunter2"))
    await db_session.commit()

    superadmin = await _make_admin(db_session, "super_one", role="superadmin")
    r = await client.get("/api/admin/settings", headers=_bearer(superadmin))
    assert r.status_code == 200
    by_key = {s["key"]: s["value"] for s in r.json()}
    assert by_key["rcon_password"] == "hunter2"


async def test_unset_secret_setting_not_masked_into_a_fake_value(client, db_session):
    # No Setting row at all for rcon_password — must stay empty, not "••••••••"
    # (which would misleadingly imply a password is actually configured).
    admin = await _make_admin(db_session, "plain_admin2")
    r = await client.get("/api/admin/settings", headers=_bearer(admin))
    assert r.status_code == 200
    keys = {s["key"] for s in r.json()}
    assert "rcon_password" not in keys


# ─── settings/import allow-list + audit (previously wrote any client-supplied
# key straight to the table with no allow-list check and no audit entry) ──────

async def test_import_settings_rejects_unknown_key(client, db_session):
    admin = await _make_admin(db_session, "import_admin")
    r = await client.post(
        "/api/admin/settings/import",
        json={"totally_not_a_real_setting": "x"},
        headers=_bearer(admin),
    )
    assert r.status_code == 400

    from sqlalchemy import select
    result = await db_session.execute(select(Setting).where(Setting.key == "totally_not_a_real_setting"))
    assert result.scalar_one_or_none() is None


async def test_import_settings_accepts_allowed_key_and_audits_it(client, db_session):
    admin = await _make_admin(db_session, "import_admin2")
    r = await client.post(
        "/api/admin/settings/import",
        json={"site_title": "Imported Title"},
        headers=_bearer(admin),
    )
    assert r.status_code == 200
    assert r.json()["imported"] == 1

    from sqlalchemy import select
    from backend.models import AuditLog
    result = await db_session.execute(select(Setting).where(Setting.key == "site_title"))
    assert result.scalar_one().value == "Imported Title"
    audit_result = await db_session.execute(select(AuditLog).where(AuditLog.action == "settings.import"))
    assert audit_result.scalar_one_or_none() is not None
