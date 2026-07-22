from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import User, GameClan, GameClanMember
from ..helpers import _get_server_names
from ..schemas import GameClanOut, GameClanDetailOut

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
            select(GameClanMember).where(GameClanMember.clan_id == clan.id).order_by(GameClanMember.character_name)
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
    query = query.order_by(GameClan.name)
    if limit:
        query = query.limit(limit)
    result = await db.execute(query)
    clans = result.scalars().all()
    server_names = await _get_server_names(db)
    return [await _game_clan_out(db, c, server_names=server_names) for c in clans]


@router.get("/api/clans/{clan_id}", response_model=GameClanDetailOut)
async def get_clan(clan_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(GameClan).where(GameClan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    return await _game_clan_out(db, clan, with_members=True)
