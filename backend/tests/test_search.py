"""Regression tests for GET /api/search — the unified sitewide search backing the
Ctrl+K global search dropdown (frontend/common.js). Covers each of the six
categories (news/player/clan/server/shop_item/event), the published/active
visibility filters it must respect (same as the endpoints it consolidates:
GET /api/news, GET /api/users, GET /api/clans), and the per-category cap."""
from datetime import datetime, timezone

import pytest

from backend.auth import get_password_hash
from backend.models import Event, GameClan, News, Setting, ShopItem, User

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


async def _make_setting(db_session, key, value):
    setting = Setting(key=key, value=value)
    db_session.add(setting)
    await db_session.commit()
    return setting


async def _make_shop_item(db_session, name, **kwargs):
    item = ShopItem(
        name=name,
        description=kwargs.pop("description", ""),
        cost=kwargs.pop("cost", 100),
        is_active=kwargs.pop("is_active", True),
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


async def _make_event(db_session, creator_id, title, **kwargs):
    event = Event(
        title=title,
        description=kwargs.pop("description", ""),
        start_date=kwargs.pop("start_date", datetime.now(timezone.utc).replace(tzinfo=None)),
        created_by=creator_id,
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)
    return event


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


async def test_search_finds_server_by_configured_name(client, db_session):
    await _make_setting(db_session, "server_name", "Crimson Wilds PvP")

    r = await client.get("/api/search", params={"q": "Crimson"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "server"
    assert body[0]["title"] == "Crimson Wilds PvP"
    assert body[0]["url"] == "/servers.html"


async def test_search_finds_second_server_by_configured_name(client, db_session):
    await _make_setting(db_session, "server2_name", "Twilight PvE")

    r = await client.get("/api/search", params={"q": "Twilight"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "server"
    assert body[0]["title"] == "Twilight PvE"


async def test_search_server_excludes_unrelated_settings(client, db_session):
    # A setting whose value happens to match but isn't a server-name key at all
    # (e.g. site_title) must never leak into results as a "server" hit.
    await _make_setting(db_session, "site_title", "Uniquephrase Site")

    r = await client.get("/api/search", params={"q": "Uniquephrase"})
    assert r.json() == []


async def test_search_finds_shop_item_by_name(client, db_session):
    await _make_shop_item(db_session, "Легендарный меч", description="Редкое оружие")

    r = await client.get("/api/search", params={"q": "Легендарный"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "shop_item"
    assert body[0]["title"] == "Легендарный меч"
    assert body[0]["url"] == "/shop.html"
    assert body[0]["snippet"] == "Редкое оружие"


async def test_search_finds_shop_item_by_description(client, db_session):
    await _make_shop_item(db_session, "Набор ресурсов", description="Содержит уникальный кристалл Moonstone")

    r = await client.get("/api/search", params={"q": "Moonstone"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "shop_item"


async def test_search_excludes_inactive_shop_items(client, db_session):
    await _make_shop_item(db_session, "Снятый с продажи предмет", is_active=False)

    r = await client.get("/api/search", params={"q": "Снятый"})
    assert r.json() == []


async def test_search_finds_event_by_title(client, db_session):
    author = await _make_user(db_session, "eventauthor")
    ev = await _make_event(db_session, author.id, "Турнир кланов", description="Ежемесячное PvP событие")

    r = await client.get("/api/search", params={"q": "Турнир"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "event"
    assert body[0]["title"] == "Турнир кланов"
    assert body[0]["url"] == f"/events.html?event={ev.id}"
    assert body[0]["snippet"] == "Ежемесячное PvP событие"


async def test_search_finds_event_by_description(client, db_session):
    author = await _make_user(db_session, "eventauthor2")
    await _make_event(db_session, author.id, "Ивент выходного дня", description="Уникальный ивент Blackwood arena")

    r = await client.get("/api/search", params={"q": "Blackwood"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "event"


async def test_search_event_no_match_returns_empty(client, db_session):
    author = await _make_user(db_session, "eventauthor3")
    await _make_event(db_session, author.id, "Событие без совпадений", description="Ничего общего")

    r = await client.get("/api/search", params={"q": "nonexistentxyz"})
    assert r.json() == []


async def test_search_caps_shop_items_per_category(client, db_session):
    for i in range(7):
        await _make_shop_item(db_session, f"Zeta Item {i}")

    r = await client.get("/api/search", params={"q": "Zeta Item"})
    body = r.json()
    assert len(body) == 5
    assert all(item["type"] == "shop_item" for item in body)


async def test_search_returns_all_six_categories_together(client, db_session):
    author = await _make_user(db_session, "vsix")
    await _make_news(db_session, author.id, "Vsix News Title", "vsix-news")
    await _make_clan(db_session, "Vsix Clan")
    await _make_setting(db_session, "server_name", "Vsix Server")
    await _make_shop_item(db_session, "Vsix Item")
    await _make_event(db_session, author.id, "Vsix Event")

    r = await client.get("/api/search", params={"q": "vsix"})
    body = r.json()
    types = {item["type"] for item in body}
    assert types == {"news", "player", "clan", "server", "shop_item", "event"}
