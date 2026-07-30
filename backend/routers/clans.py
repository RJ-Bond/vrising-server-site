from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case

from ..database import get_db
from ..models import User, GameClan, GameClanMember
from ..helpers import _get_server_names
from ..schemas import GameClanOut, GameClanDetailOut

# Leaders/officers first, then alphabetical by character name — used both for
# GET /api/clans's small per-card member preview and GET /api/clans/{id}'s full list.
_ROLE_RANK = case((GameClanMember.role == "leader", 0), (GameClanMember.role == "officer", 1), else_=2)
_MEMBER_PREVIEW_SIZE = 4

router = APIRouter()


# ─── Clans (game-synced, read-only) ───────────────────────────────────────────
# Clan data is owned by the game itself — the plugin pushes the full current roster to
# POST /api/plugin/clans/sync (see "Game Plugin Integration" above). The website only
# ever displays it; there is no web-managed create/join/leave/delete anymore.

async def _game_clan_out(db: AsyncSession, clan: GameClan, with_members: bool = False, server_names: Optional[dict] = None):
    count_result = await db.execute(
        select(func.count(GameClanMember.id)).where(GameClanMember.clan_id == clan.id)
    )
    member_count = count_result.scalar_one()
    if server_names is None:
        server_names = await _get_server_names(db)
    base = {
        "id": clan.id, "server_num": clan.server_num, "clan_guid": clan.clan_guid,
        "server_name": server_names.get(clan.server_num) or f"Сервер {clan.server_num}",
        "name": clan.name, "motto": clan.motto or "", "updated_at": clan.updated_at,
        "member_count": member_count,
    }
    if with_members:
        members_result = await db.execute(
            select(GameClanMember).where(GameClanMember.clan_id == clan.id)
            .order_by(_ROLE_RANK, GameClanMember.character_name)
        )
        members = members_result.scalars().all()
        steam_ids = [m.steam_id for m in members]
        users_by_steam = {}
        if steam_ids:
            users_result = await db.execute(select(User).where(User.steam_id.in_(steam_ids)))
            users_by_steam = {u.steam_id: u for u in users_result.scalars().all()}
        member_list = []
        for m in members:
            u = users_by_steam.get(m.steam_id)
            member_list.append({
                "steam_id": m.steam_id, "character_name": m.character_name, "role": m.role,
                "username": u.username if u else None,
                "avatar_url": u.avatar_url if u else None,
            })
        base["members"] = member_list
    return base


@router.get("/api/clans", response_model=list[GameClanOut])
async def list_clans(search: Optional[str] = None, limit: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    query = select(GameClan)
    if search:
        query = query.where(GameClan.name.ilike(f"%{search}%"))
    result = await db.execute(query)
    clans = result.scalars().all()
    if not clans:
        return []
    clan_ids = [c.id for c in clans]
    server_names = await _get_server_names(db)

    # Member counts for every clan in one grouped query, instead of the one-COUNT-
    # query-per-clan _game_clan_out does — this endpoint can return 100+ clans, and that
    # was 100+ round-trips for a single public page load.
    count_rows = (await db.execute(
        select(GameClanMember.clan_id, func.count(GameClanMember.id))
        .where(GameClanMember.clan_id.in_(clan_ids)).group_by(GameClanMember.clan_id)
    )).all()
    counts = dict(count_rows)

    # Small member preview (up to _MEMBER_PREVIEW_SIZE, leaders/officers first) per clan
    # for the public list's card avatar-stack — one bulk query for every clan's members
    # instead of a separate round-trip per clan, then capped to the preview size in
    # Python (SQL "top N per group" needs a window function SQLite support is spotty
    # for; at this row count, filtering here is simpler and plenty fast).
    all_members = (await db.execute(
        select(GameClanMember).where(GameClanMember.clan_id.in_(clan_ids))
        .order_by(GameClanMember.clan_id, _ROLE_RANK, GameClanMember.character_name)
    )).scalars().all()
    steam_ids = list({m.steam_id for m in all_members})
    users_by_steam = {}
    if steam_ids:
        users_by_steam = {
            u.steam_id: u for u in
            (await db.execute(select(User).where(User.steam_id.in_(steam_ids)))).scalars().all()
        }
    previews_by_clan: dict[int, list[dict]] = {}
    for m in all_members:
        bucket = previews_by_clan.setdefault(m.clan_id, [])
        if len(bucket) >= _MEMBER_PREVIEW_SIZE:
            continue
        u = users_by_steam.get(m.steam_id)
        bucket.append({
            "steam_id": m.steam_id, "character_name": m.character_name, "role": m.role,
            "username": u.username if u else None,
            "avatar_url": u.avatar_url if u else None,
        })

    out = [
        {
            "id": c.id, "server_num": c.server_num, "clan_guid": c.clan_guid,
            "server_name": server_names.get(c.server_num) or f"Сервер {c.server_num}",
            "name": c.name, "motto": c.motto or "", "updated_at": c.updated_at,
            "member_count": counts.get(c.id, 0),
            "member_preview": previews_by_clan.get(c.id, []),
        }
        for c in clans
    ]
    # Real communities first, not alphabetical: V Rising lets anyone spin up a clan
    # trivially, and on production ~1 in 5 synced clans has 0 members (abandoned or a
    # throwaway) and dozens more are unnamed test clutter ("1", "123", literally "clan"
    # x13) — sorted alphabetically, that noise dominated the top of the page ahead of
    # every active clan. A 0-member clan isn't a community yet, so it's hidden outright
    # rather than just sorted last.
    out = [c for c in out if c["member_count"] > 0]
    out.sort(key=lambda c: c["member_count"], reverse=True)
    if limit:
        out = out[:limit]
    return out


@router.get("/api/clans/{clan_id}", response_model=GameClanDetailOut)
async def get_clan(clan_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(GameClan).where(GameClan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    return await _game_clan_out(db, clan, with_members=True)
