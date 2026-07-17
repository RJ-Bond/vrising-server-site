"""Regression tests for GET /api/news-embed — the server-rendered <head> meta used
for crawlers that don't execute JS (Discord/Telegram/VK/Twitter unfurlers, most search
bots), since they never see index.js's client-side setMeta() call and would otherwise
always see the generic homepage title/description/image for every shared article link."""
from pathlib import Path

import pytest

from backend.models import News, User
from backend.auth import get_password_hash

pytestmark = pytest.mark.asyncio

_REAL_INDEX_HTML = str(Path(__file__).resolve().parent.parent.parent / "frontend" / "index.html")


async def _make_author(db_session):
    user = User(username="author1", email="author1@example.com", hashed_password=get_password_hash("x"))
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_news_embed_swaps_article_meta(client, db_session, monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main, "_INDEX_HTML_PATH", _REAL_INDEX_HTML)

    author = await _make_author(db_session)
    news = News(
        title="Большое обновление сервера",
        slug="big-update",
        summary="Что нового в этом патче",
        content="<p>Подробности патча...</p>",
        thumbnail_url="/uploads/patch.png",
        author_id=author.id,
        published=True,
    )
    db_session.add(news)
    await db_session.commit()

    r = await client.get("/api/news-embed", params={"slug": "big-update"})
    assert r.status_code == 200
    body = r.text
    assert "Большое обновление сервера" in body
    assert "Что нового в этом патче" in body
    assert "/?news=big-update" in body
    assert "/uploads/patch.png" in body
    assert '<meta property="og:type" content="article" />' in body
    # The default homepage meta must actually be gone, not just appended alongside it.
    assert "Just-Skill.Ru — Игровое сообщество V Rising</title>" not in body


async def test_news_embed_unknown_slug_falls_back_to_default_meta(client, db_session, monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main, "_INDEX_HTML_PATH", _REAL_INDEX_HTML)

    r = await client.get("/api/news-embed", params={"slug": "does-not-exist"})
    assert r.status_code == 200
    assert "Just-Skill.Ru — Игровое сообщество V Rising</title>" in r.text


async def test_news_embed_unpublished_falls_back_to_default_meta(client, db_session, monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main, "_INDEX_HTML_PATH", _REAL_INDEX_HTML)

    author = await _make_author(db_session)
    news = News(
        title="Черновик", slug="draft-1", summary="s", content="c",
        author_id=author.id, published=False,
    )
    db_session.add(news)
    await db_session.commit()

    r = await client.get("/api/news-embed", params={"slug": "draft-1"})
    assert r.status_code == 200
    assert "Черновик" not in r.text


async def test_news_embed_missing_index_html_returns_404(client, db_session, monkeypatch):
    import backend.main as main
    monkeypatch.setattr(main, "_INDEX_HTML_PATH", "/nonexistent/index.html")

    r = await client.get("/api/news-embed", params={"slug": "anything"})
    assert r.status_code == 404
