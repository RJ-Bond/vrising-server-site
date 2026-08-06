"""Regression tests for the opt-in email newsletter/digest feature:
- PUT /api/profile/newsletter (backend/routers/profile.py) — the opt-in toggle.
- send_newsletter_digest() (backend/helpers.py) — selects opted-in/active users and
  news published since the last send, and calls the (mocked) SMTP sender.
- POST /api/admin/newsletter/send-now (backend/routers/admin_misc.py) — the
  admin-tier manual trigger, which just calls send_newsletter_digest() on demand.

Never exercises real SMTP: backend.helpers._send_notification_email is monkeypatched
in every test that would otherwise reach it, same approach as test_push_notifications.py
monkeypatching helpers.webpush.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import backend.helpers as helpers
from backend.auth import create_access_token, get_password_hash
from backend.models import News, Setting, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user", newsletter_opt_in=False, is_active=True):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
        newsletter_opt_in=newsletter_opt_in,
        is_active=is_active,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_news(db_session, author, slug, *, published=True, is_template=False, created_at=None, title=None, summary=None):
    news = News(
        title=title or f"News {slug}",
        slug=slug,
        summary=summary or f"Summary for {slug}",
        content="<p>content</p>",
        author_id=author.id,
        published=published,
        is_template=is_template,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    return news


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


def _fake_sender(calls):
    async def _send(to_email, subject, body_text, body_html):
        calls.append({"to": to_email, "subject": subject, "text": body_text, "html": body_html})
        return True
    return _send


# ─── PUT /api/profile/newsletter ─────────────────────────────────────────────

async def test_newsletter_toggle_requires_login(client, db_session):
    r = await client.put("/api/profile/newsletter", json={"newsletter_opt_in": True})
    assert r.status_code == 401


async def test_newsletter_toggle_persists_on_and_off(client, db_session):
    user = await _make_user(db_session, "NewsletterUser1")
    assert user.newsletter_opt_in is False

    r = await client.put("/api/profile/newsletter", headers=_bearer(user), json={"newsletter_opt_in": True})
    assert r.status_code == 200
    assert r.json() == {"newsletter_opt_in": True}

    # db_session already has `user` in its identity map from _make_user(), and the
    # PUT above committed through a *different* session (the request's own, via
    # Depends(get_db)) — expire_on_commit=False means db_session won't automatically
    # see the new value on a plain re-SELECT, so refresh() explicitly (same pattern
    # as test_admin_settings.py / test_admin_role_tiers.py).
    await db_session.refresh(user)
    assert user.newsletter_opt_in is True

    # Same toggle, other direction — must be just as easy to turn back off.
    r2 = await client.put("/api/profile/newsletter", headers=_bearer(user), json={"newsletter_opt_in": False})
    assert r2.status_code == 200
    assert r2.json() == {"newsletter_opt_in": False}

    await db_session.refresh(user)
    assert user.newsletter_opt_in is False


async def test_me_reflects_newsletter_opt_in(client, db_session):
    user = await _make_user(db_session, "NewsletterUser2", newsletter_opt_in=True)
    r = await client.get("/api/auth/me", headers=_bearer(user))
    assert r.status_code == 200
    assert r.json()["newsletter_opt_in"] is True


# ─── send_newsletter_digest() ────────────────────────────────────────────────

async def test_digest_sends_only_to_opted_in_active_users(db_session, monkeypatch):
    author = await _make_user(db_session, "NewsAuthor1", role="admin")
    opted_in = await _make_user(db_session, "OptedIn1", newsletter_opt_in=True)
    opted_out = await _make_user(db_session, "OptedOut1", newsletter_opt_in=False)
    inactive_opted_in = await _make_user(db_session, "InactiveOptedIn1", newsletter_opt_in=True, is_active=False)
    await _make_news(db_session, author, "digest-news-1")

    calls = []
    monkeypatch.setattr(helpers, "_send_notification_email", _fake_sender(calls))

    summary = await helpers.send_newsletter_digest(db_session)

    assert summary == {"items": 1, "recipients": 1, "sent": 1}
    recipients = {c["to"] for c in calls}
    assert recipients == {opted_in.email}
    assert opted_out.email not in recipients
    assert inactive_opted_in.email not in recipients


async def test_digest_excludes_unpublished_and_template_news(db_session, monkeypatch):
    author = await _make_user(db_session, "NewsAuthor2", role="admin")
    await _make_user(db_session, "OptedIn2", newsletter_opt_in=True)
    await _make_news(db_session, author, "digest-published", published=True)
    await _make_news(db_session, author, "digest-draft", published=False)
    await _make_news(db_session, author, "digest-template", published=True, is_template=True)

    calls = []
    monkeypatch.setattr(helpers, "_send_notification_email", _fake_sender(calls))

    summary = await helpers.send_newsletter_digest(db_session)

    assert summary["items"] == 1
    assert "digest-published" in calls[0]["html"]
    assert "digest-draft" not in calls[0]["html"]
    assert "digest-template" not in calls[0]["html"]


async def test_digest_first_run_looks_back_only_7_days(db_session, monkeypatch):
    author = await _make_user(db_session, "NewsAuthor3", role="admin")
    await _make_user(db_session, "OptedIn3", newsletter_opt_in=True)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    await _make_news(db_session, author, "digest-old", created_at=old)
    await _make_news(db_session, author, "digest-recent", created_at=recent)

    calls = []
    monkeypatch.setattr(helpers, "_send_notification_email", _fake_sender(calls))

    summary = await helpers.send_newsletter_digest(db_session)

    assert summary["items"] == 1
    assert "digest-recent" in calls[0]["html"]
    assert "digest-old" not in calls[0]["html"]


async def test_digest_advances_cutoff_so_second_run_finds_nothing_new(db_session, monkeypatch):
    author = await _make_user(db_session, "NewsAuthor4", role="admin")
    await _make_user(db_session, "OptedIn4", newsletter_opt_in=True)
    await _make_news(db_session, author, "digest-once")

    calls = []
    monkeypatch.setattr(helpers, "_send_notification_email", _fake_sender(calls))

    first = await helpers.send_newsletter_digest(db_session)
    assert first == {"items": 1, "recipients": 1, "sent": 1}

    second = await helpers.send_newsletter_digest(db_session)
    assert second == {"items": 0, "recipients": 0, "sent": 0}
    assert len(calls) == 1  # no duplicate send on the second run

    setting = (await db_session.execute(
        select(Setting).where(Setting.key == helpers.NEWSLETTER_LAST_SENT_SETTING_KEY)
    )).scalar_one_or_none()
    assert setting is not None and setting.value


async def test_digest_no_op_when_nobody_opted_in(db_session, monkeypatch):
    author = await _make_user(db_session, "NewsAuthor5", role="admin")
    await _make_news(db_session, author, "digest-lonely")

    calls = []
    monkeypatch.setattr(helpers, "_send_notification_email", _fake_sender(calls))

    summary = await helpers.send_newsletter_digest(db_session)
    assert summary == {"items": 1, "recipients": 0, "sent": 0}
    assert calls == []


async def test_digest_swallows_per_recipient_send_failure(db_session, monkeypatch):
    author = await _make_user(db_session, "NewsAuthor6", role="admin")
    await _make_user(db_session, "OptedInFail", newsletter_opt_in=True)
    await _make_news(db_session, author, "digest-fail-case")

    async def _boom(*args, **kwargs):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(helpers, "_send_notification_email", _boom)

    # Must not raise even though the only recipient's send blows up.
    summary = await helpers.send_newsletter_digest(db_session)
    assert summary == {"items": 1, "recipients": 1, "sent": 0}


# ─── POST /api/admin/newsletter/send-now ─────────────────────────────────────

async def test_manual_trigger_requires_login(client, db_session):
    r = await client.post("/api/admin/newsletter/send-now")
    assert r.status_code == 401


async def test_manual_trigger_forbidden_for_plain_user(client, db_session):
    user = await _make_user(db_session, "PlainUser1", role="user")
    r = await client.post("/api/admin/newsletter/send-now", headers=_bearer(user))
    assert r.status_code == 403


async def test_manual_trigger_forbidden_for_moderator(client, db_session):
    mod = await _make_user(db_session, "ModUser1", role="moderator")
    r = await client.post("/api/admin/newsletter/send-now", headers=_bearer(mod))
    assert r.status_code == 403


async def test_manual_trigger_allowed_for_admin_and_calls_shared_logic(client, db_session, monkeypatch):
    admin = await _make_user(db_session, "AdminUser1", role="admin")
    recipient = await _make_user(db_session, "OptedInManual", newsletter_opt_in=True)
    await _make_news(db_session, admin, "digest-manual-trigger")

    calls = []
    monkeypatch.setattr(helpers, "_send_notification_email", _fake_sender(calls))

    r = await client.post("/api/admin/newsletter/send-now", headers=_bearer(admin))
    assert r.status_code == 200
    assert r.json() == {"items": 1, "recipients": 1, "sent": 1}
    assert calls and calls[0]["to"] == recipient.email


async def test_manual_trigger_allowed_for_superadmin(client, db_session, monkeypatch):
    superadmin = await _make_user(db_session, "SuperAdminUser1", role="superadmin")
    monkeypatch.setattr(helpers, "_send_notification_email", _fake_sender([]))
    r = await client.post("/api/admin/newsletter/send-now", headers=_bearer(superadmin))
    assert r.status_code == 200
