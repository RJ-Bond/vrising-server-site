"""GET /api/search?q=... — unified sitewide search across news, players (site
accounts), and clans, backing the Ctrl+K global search dropdown
(frontend/common.js's openGlobalSearch()/_doSearch()). Before this endpoint
existed, that dropdown fanned out to three separate endpoints itself
(/api/users, /api/news, /api/clans, each with their own ?search= param — see
the "Public user search" comment in routers/users.py); this consolidates that
into one round-trip and adds a `snippet` field none of those three expose
uniformly. The three original endpoints are left as-is (still used elsewhere:
/api/news?search= backs the news page's own filter box, /api/clans?search=
the clans page's, /api/users?search= nothing else yet) — this is additive,
not a replacement for them.

Public, unauthenticated — same visibility as the leaderboard/clans list/news
feed, all of which already show this data to anyone. Capped to a handful of
rows per category since this fires on every keystroke (debounced
client-side) and only needs to feed a small dropdown, not a full
search-results page — so no pagination, no total counts.
"""
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import GameClan, News, User
from ..rate_limit import limiter

router = APIRouter()

_PER_CATEGORY = 5


class SearchResultOut(BaseModel):
    type: Literal["news", "player", "clan"]
    title: str
    url: str
    snippet: str = ""


@router.get("/api/search", response_model=list[SearchResultOut])
@limiter.limit("30/minute")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    term = q.strip()
    if not term:
        return []
    like = f"%{term}%"
    results: list[SearchResultOut] = []

    # News: match title or body, but show the pre-written summary (not raw HTML
    # content) as the snippet — same short plain-text field NewsListOut already
    # exposes for the news list cards.
    news_rows = (await db.execute(
        select(News.title, News.slug, News.summary)
        .where(News.published == True, or_(News.title.ilike(like), News.content.ilike(like)))  # noqa: E712 — SQLAlchemy idiom, see pyproject.toml
        .order_by(News.pinned.desc(), News.created_at.desc())
        .limit(_PER_CATEGORY)
    )).all()
    for title, slug, summary in news_rows:
        results.append(SearchResultOut(
            type="news", title=title, url=f"/?news={quote(slug)}", snippet=(summary or "").strip(),
        ))

    # Players: site accounts, matched on username or in-game nickname — same
    # columns /api/users?search= already matches on (routers/users.py), kept in
    # sync deliberately. Inactive (banned) accounts are excluded, same as that
    # endpoint and the public profile lookup.
    player_rows = (await db.execute(
        select(User.username, User.game_nickname, User.role)
        .where(User.is_active == True, or_(User.username.ilike(like), User.game_nickname.ilike(like)))  # noqa: E712
        .order_by(User.username)
        .limit(_PER_CATEGORY)
    )).all()
    for username, game_nickname, role in player_rows:
        snippet = role if role and role != "user" else (game_nickname or "")
        results.append(SearchResultOut(
            type="player", title=username, url=f"/user.html?u={quote(username)}", snippet=snippet,
        ))

    # Clans: game-synced roster (GameClan is the source of truth — see the
    # docstring on that model), matched on name. Includes the clan id in the URL
    # so clans.html's existing ?clan=<id> deep link (used to open the detail
    # modal directly) works from a search result, unlike the plain /clans.html
    # link the old 3-endpoint dropdown used.
    clan_rows = (await db.execute(
        select(GameClan.id, GameClan.name, GameClan.motto)
        .where(GameClan.name.ilike(like))
        .order_by(GameClan.name)
        .limit(_PER_CATEGORY)
    )).all()
    for clan_id, name, motto in clan_rows:
        results.append(SearchResultOut(
            type="clan", title=name, url=f"/clans.html?clan={clan_id}", snippet=(motto or "").strip(),
        ))

    return results
