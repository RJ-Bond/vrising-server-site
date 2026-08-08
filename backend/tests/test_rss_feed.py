"""Regression tests for GET /api/rss.xml (backend/main.py) — the RSS 2.0 feed of
published news, proxied at /rss.xml by nginx (see nginx/nginx*.conf) and linked from
frontend/index.html's <head> via <link rel="alternate" type="application/rss+xml">.
Covers well-formedness (the endpoint hand-builds XML via string formatting rather than
a library, so a malformed escape would silently produce a feed no reader can parse) and
that title/description text containing XML metacharacters (&, <, >, quotes) — e.g. from
news content pulled straight out of the DB — round-trips safely instead of breaking the
document or opening an injection path."""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import get_password_hash
from backend.models import Event, News, User

pytestmark = pytest.mark.asyncio


async def _make_author(db_session):
    user = User(username="feedauthor", email="feedauthor@example.com", hashed_password=get_password_hash("x"))
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_event(db_session, creator, title="Feed Test Event", status="upcoming", description="desc"):
    ev = Event(
        title=title,
        description=description,
        event_type="tournament",
        start_date=datetime.now(timezone.utc) + timedelta(days=2),
        status=status,
        created_by=creator.id,
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)
    return ev


async def test_rss_feed_well_formed_with_no_news(client, db_session):
    r = await client.get("/api/rss.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    root = ET.fromstring(r.text)  # raises ET.ParseError if malformed
    assert root.tag == "rss"
    channel = root.find("channel")
    assert channel is not None
    assert channel.find("title") is not None


async def test_rss_feed_includes_published_news(client, db_session):
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

    r = await client.get("/api/rss.xml")
    root = ET.fromstring(r.text)
    items = root.find("channel").findall("item")
    assert len(items) == 1
    assert items[0].find("title").text == "Обновление сервера"
    assert items[0].find("link").text.endswith("/?news=server-update")
    # Content HTML tags must be stripped from the description, not dumped raw.
    assert "<p>" not in items[0].find("description").text
    assert "Подробности патча." in items[0].find("description").text
    # pubDate must be RFC 822 (ends in a timezone/offset like "+0000").
    assert items[0].find("pubDate").text.endswith("+0000")


async def test_rss_feed_excludes_unpublished_news(client, db_session):
    author = await _make_author(db_session)
    news = News(
        title="Черновик", slug="draft", summary="s", content="c",
        author_id=author.id, published=False,
    )
    db_session.add(news)
    await db_session.commit()

    r = await client.get("/api/rss.xml")
    root = ET.fromstring(r.text)
    assert root.find("channel").findall("item") == []


async def test_rss_feed_escapes_ampersand_and_angle_brackets_in_title(client, db_session):
    author = await _make_author(db_session)
    news = News(
        title='Patch <1.2> & "special" chars',
        slug="patch-special-chars",
        summary="s",
        content="c",
        author_id=author.id,
        published=True,
    )
    db_session.add(news)
    await db_session.commit()

    r = await client.get("/api/rss.xml")
    # The raw response body must not contain an unescaped "<1.2>" — that would either
    # break XML parsing or, worse, be interpreted as a tag by a lax feed reader.
    assert "<1.2>" not in r.text
    assert "&amp;" in r.text

    root = ET.fromstring(r.text)  # would raise ET.ParseError if the escaping were broken
    title_text = root.find("channel").find("item").find("title").text
    assert title_text == 'Patch <1.2> & "special" chars'


async def test_rss_feed_escapes_ampersand_in_content_description(client, db_session):
    author = await _make_author(db_session)
    news = News(
        title="Title",
        slug="amp-in-content",
        summary="s",
        content="<p>Rock & Stone, forever & ever</p>",
        author_id=author.id,
        published=True,
    )
    db_session.add(news)
    await db_session.commit()

    r = await client.get("/api/rss.xml")
    root = ET.fromstring(r.text)
    desc = root.find("channel").find("item").find("description").text
    assert desc == "Rock & Stone, forever & ever"


# ─── Events merged into the feed ─────────────────────────────────────────────
# GET /api/rss.xml originally only ever emitted News items; it was extended to also
# include upcoming/active Events as additional <item>s in the same reverse-chronological
# list (see the comment above the event query in main.py's rss_feed() for the exact
# inclusion rule and why pubDate is created_at rather than start_date).

async def test_rss_feed_includes_upcoming_and_active_events(client, db_session):
    creator = await _make_author(db_session)
    upcoming = await _make_event(db_session, creator, title="Upcoming Arena Event", status="upcoming")
    active = await _make_event(db_session, creator, title="Active Wipe Event", status="active")

    r = await client.get("/api/rss.xml")
    root = ET.fromstring(r.text)
    items = root.find("channel").findall("item")
    titles = [i.find("title").text for i in items]
    assert "Upcoming Arena Event" in titles
    assert "Active Wipe Event" in titles
    links = [i.find("link").text for i in items]
    assert any(link.endswith(f"/events.html?event={upcoming.id}") for link in links)
    assert any(link.endswith(f"/events.html?event={active.id}") for link in links)


async def test_rss_feed_excludes_ended_and_cancelled_events(client, db_session):
    creator = await _make_author(db_session)
    await _make_event(db_session, creator, title="Long Over Event", status="ended")
    await _make_event(db_session, creator, title="Scrapped Event", status="cancelled")

    r = await client.get("/api/rss.xml")
    root = ET.fromstring(r.text)
    titles = [i.find("title").text for i in root.find("channel").findall("item")]
    assert "Long Over Event" not in titles
    assert "Scrapped Event" not in titles


async def test_rss_feed_interleaves_news_and_events_reverse_chronologically(client, db_session):
    author = await _make_author(db_session)
    now = datetime.now(timezone.utc)

    older_news = News(
        title="Older News", slug="older-news", summary="s", content="c",
        author_id=author.id, published=True, created_at=now - timedelta(hours=2),
    )
    middle_event = Event(
        title="Middle Event", description="d", event_type="event",
        start_date=now + timedelta(days=1), status="upcoming",
        created_by=author.id, created_at=now - timedelta(hours=1),
    )
    newest_news = News(
        title="Newest News", slug="newest-news", summary="s", content="c",
        author_id=author.id, published=True, created_at=now,
    )
    db_session.add_all([older_news, middle_event, newest_news])
    await db_session.commit()

    r = await client.get("/api/rss.xml")
    root = ET.fromstring(r.text)
    titles = [i.find("title").text for i in root.find("channel").findall("item")]
    # Reverse-chronological by pubDate, news and events sharing one ordered list rather
    # than events appearing as a separate trailing/leading block.
    assert titles.index("Newest News") < titles.index("Middle Event") < titles.index("Older News")
