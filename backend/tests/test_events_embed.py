"""Regression tests for GET /api/events-embed — the server-rendered <head> meta used
for crawlers that don't execute JS (Discord/Telegram/VK/Twitter unfurlers, most search
bots), mirroring test_news_embed.py's structure for GET /api/news-embed. Without this,
a shared event link always showed the generic events-list title/description/image no
matter which event it was."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backend.routers.events as events_router
from backend.models import Event, User
from backend.auth import get_password_hash

pytestmark = pytest.mark.asyncio

_REAL_EVENTS_HTML = str(Path(__file__).resolve().parent.parent.parent / "frontend" / "events.html")


async def _make_creator(db_session):
    user = User(username="eventcreator1", email="eventcreator1@example.com", hashed_password=get_password_hash("x"))
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_events_embed_swaps_event_meta(client, db_session, monkeypatch):
    monkeypatch.setattr(events_router, "_EVENTS_HTML_PATH", _REAL_EVENTS_HTML)

    creator = await _make_creator(db_session)
    ev = Event(
        title="Кровавая луна: турнир",
        description="Большой PvP турнир на арене",
        event_type="tournament",
        start_date=datetime.now(timezone.utc) + timedelta(days=3),
        cover_url="/uploads/event-cover.png",
        created_by=creator.id,
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)

    r = await client.get("/api/events-embed", params={"id": ev.id})
    assert r.status_code == 200
    body = r.text
    assert "Кровавая луна: турнир" in body
    assert "Большой PvP турнир на арене" in body
    assert f"/events.html?event={ev.id}" in body
    assert "/uploads/event-cover.png" in body
    # The default events-list meta must actually be gone, not just appended alongside it.
    assert "События — V Rising</title>" not in body


async def test_events_embed_unknown_id_falls_back_to_default_meta(client, db_session, monkeypatch):
    monkeypatch.setattr(events_router, "_EVENTS_HTML_PATH", _REAL_EVENTS_HTML)

    r = await client.get("/api/events-embed", params={"id": 999999})
    assert r.status_code == 200
    assert "События — V Rising</title>" in r.text


async def test_events_embed_missing_events_html_returns_404(client, db_session, monkeypatch):
    monkeypatch.setattr(events_router, "_EVENTS_HTML_PATH", "/nonexistent/events.html")

    r = await client.get("/api/events-embed", params={"id": 1})
    assert r.status_code == 404
