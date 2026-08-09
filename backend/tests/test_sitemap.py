"""Regression tests for GET /api/sitemap.xml (backend/main.py). Originally only listed
static page URLs plus one entry per published News article (/?news=<slug>) and a single
generic /events.html entry — individual events weren't listed the deep-link way
(/events.html?event=<id>, the same URL shape used by the events-embed crawler route and
the RSS feed's event items). Covers that upcoming/active events now get their own entry
(same status filter as the RSS feed's event inclusion — see test_rss_feed.py) and that
ended/cancelled events are excluded as stale noise."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import get_password_hash
from backend.models import Event, News, User

pytestmark = pytest.mark.asyncio


async def _make_author(db_session):
    user = User(username="sitemapauthor", email="sitemapauthor@example.com", hashed_password=get_password_hash("x"))
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_event(db_session, creator, title="Sitemap Test Event", status="upcoming"):
    ev = Event(
        title=title,
        description="desc",
        event_type="tournament",
        start_date=datetime.now(timezone.utc) + timedelta(days=2),
        status=status,
        created_by=creator.id,
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)
    return ev


async def test_sitemap_well_formed_with_no_content(client, db_session):
    r = await client.get("/api/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<urlset" in r.text
    assert "/events.html</loc>" in r.text  # generic events listing entry still present


async def test_sitemap_includes_upcoming_and_active_events(client, db_session):
    creator = await _make_author(db_session)
    upcoming = await _make_event(db_session, creator, title="Upcoming Arena Event", status="upcoming")
    active = await _make_event(db_session, creator, title="Active Wipe Event", status="active")

    r = await client.get("/api/sitemap.xml")
    assert f"/events.html?event={upcoming.id}</loc>" in r.text
    assert f"/events.html?event={active.id}</loc>" in r.text


async def test_sitemap_excludes_ended_and_cancelled_events(client, db_session):
    creator = await _make_author(db_session)
    ended = await _make_event(db_session, creator, title="Long Over Event", status="ended")
    cancelled = await _make_event(db_session, creator, title="Scrapped Event", status="cancelled")

    r = await client.get("/api/sitemap.xml")
    assert f"/events.html?event={ended.id}</loc>" not in r.text
    assert f"/events.html?event={cancelled.id}</loc>" not in r.text


async def test_sitemap_includes_published_news(client, db_session):
    author = await _make_author(db_session)
    news = News(
        title="Обновление сервера",
        slug="server-update",
        summary="Что нового",
        content="<p>Подробности патча.</p>",
        author_id=author.id,
        published=True,
    )
    db_session.add(news)
    await db_session.commit()

    r = await client.get("/api/sitemap.xml")
    assert "/?news=server-update</loc>" in r.text
