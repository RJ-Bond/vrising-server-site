"""Regression tests for GET /api/search — the unified sitewide search backing the
Ctrl+K global search dropdown (frontend/common.js). Covers each of the three
categories (news/player/clan), the published/active visibility filters it must
respect (same as the endpoints it consolidates: GET /api/news, GET /api/users,
GET /api/clans), and the per-category cap."""
import pytest

from backend.auth import get_password_hash
from backend.models import GameClan, News, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, **kwargs):
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("x"),
        **kwargs,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_news(db_session, author_id, title, slug, **kwargs):
    news = News(
        title=title,
        slug=slug,
        summary=kwargs.pop("summary", "Краткое описание"),
        content=kwargs.pop("content", "<p>Текст новости</p>"),
        author_id=author_id,
        published=kwargs.pop("published", True),
        **kwargs,
    )
    db_session.add(news)
    await db_session.commit()
    return news


async def _make_clan(db_session, name, **kwargs):
    clan = GameClan(
        server_num=kwargs.pop("server_num", 1),
        clan_guid=kwargs.pop("clan_guid", f"guid-{name}"),
        name=name,
        motto=kwargs.pop("motto", ""),
    )
    db_session.add(clan)
    await db_session.commit()
    await db_session.refresh(clan)
    return clan


async def test_search_requires_q_param(client, db_session):
    r = await client.get("/api/search")
    assert r.status_code == 422


async def test_search_no_matches_returns_empty_list(client, db_session):
    r = await client.get("/api/search", params={"q": "nonexistentxyz"})
    assert r.status_code == 200
    assert r.json() == []


async def test_search_finds_news_by_title(client, db_session):
    author = await _make_user(db_session, "author1")
    await _make_news(db_session, author.id, "Большое обновление сервера", "big-update")

    r = await client.get("/api/search", params={"q": "обновление"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "news"
    assert body[0]["title"] == "Большое обновление сервера"
    assert body[0]["url"] == "/?news=big-update"
    assert body[0]["snippet"] == "Краткое описание"


async def test_search_finds_news_by_content_not_just_title(client, db_session):
    author = await _make_user(db_session, "author2")
    await _make_news(
        db_session, author.id, "Патч 1.2", "patch-1-2",
        content="<p>В этом патче добавлен уникальный предмет Blackfang Sword.</p>",
    )

    r = await client.get("/api/search", params={"q": "Blackfang"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "news"


async def test_search_excludes_unpublished_news(client, db_session):
    author = await _make_user(db_session, "author3")
    await _make_news(db_session, author.id, "Черновик статьи", "draft-article", published=False)

    r = await client.get("/api/search", params={"q": "Черновик"})
    assert r.json() == []


async def test_search_finds_player_by_username(client, db_session):
    await _make_user(db_session, "Vortigern")

    r = await client.get("/api/search", params={"q": "vortig"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "player"
    assert body[0]["title"] == "Vortigern"
    assert body[0]["url"] == "/user.html?u=Vortigern"


async def test_search_finds_player_by_game_nickname(client, db_session):
    await _make_user(db_session, "sitehandle", game_nickname="DracarysIRL")

    r = await client.get("/api/search", params={"q": "Dracarys"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "player"
    assert body[0]["title"] == "sitehandle"


async def test_search_excludes_inactive_users(client, db_session):
    await _make_user(db_session, "bannedplayer", is_active=False)

    r = await client.get("/api/search", params={"q": "bannedplayer"})
    assert r.json() == []


async def test_search_finds_clan_by_name(client, db_session):
    await _make_clan(db_session, "Кровавые Клыки", motto="Старейший клан сервера")

    r = await client.get("/api/search", params={"q": "Кровавые"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "clan"
    assert body[0]["title"] == "Кровавые Клыки"
    assert body[0]["snippet"] == "Старейший клан сервера"


async def test_search_username_with_space_is_url_encoded(client, db_session):
    await _make_user(db_session, "Player One")

    r = await client.get("/api/search", params={"q": "Player One"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["url"] == "/user.html?u=Player%20One"


async def test_search_caps_results_per_category(client, db_session):
    await _make_clan(db_session, "Alpha Clan 1")
    for i in range(7):
        await _make_clan(db_session, f"Alpha Clan {i + 2}", clan_guid=f"guid-alpha-{i}")

    r = await client.get("/api/search", params={"q": "Alpha Clan"})
    body = r.json()
    assert len(body) == 5
    assert all(item["type"] == "clan" for item in body)


async def test_search_returns_all_three_categories_together(client, db_session):
    author = await _make_user(db_session, "vshared")
    await _make_news(db_session, author.id, "Vshared News Title", "vshared-news")
    await _make_clan(db_session, "Vshared Clan")

    r = await client.get("/api/search", params={"q": "vshared"})
    body = r.json()
    types = {item["type"] for item in body}
    assert types == {"news", "player", "clan"}
