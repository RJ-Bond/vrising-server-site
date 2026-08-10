import html
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case

from ..database import get_db
from ..models import User, GameClan, GameClanMember, GameClanBase, ClanMembershipEvent, Setting
from ..auth import get_current_user
from ..helpers import _get_server_names
from ..schemas import GameClanOut, GameClanDetailOut, GameClanLeaderboardOut, ClanMembershipEventOut

# Leaders/officers first, then alphabetical by character name — used both for
# GET /api/clans's small per-card member preview and GET /api/clans/{id}'s full list.
_ROLE_RANK = case((GameClanMember.role == "leader", 0), (GameClanMember.role == "officer", 1), else_=2)
_MEMBER_PREVIEW_SIZE = 4
# Cap for GET /api/clans/leaderboard — clan counts here are the same order of magnitude
# as GET /api/clans (dozens, not thousands; see that endpoint's "1 in 5 has 0 members"
# comment), so a plain top-N with no pagination is plenty rather than adding page params
# for a list this small.
_LEADERBOARD_DEFAULT_LIMIT = 20
_LEADERBOARD_MAX_LIMIT = 50
# Cap for GET /api/clans/{id}/history — a small recent-activity list, not a full
# paginated audit log.
_HISTORY_DEFAULT_LIMIT = 30
_HISTORY_MAX_LIMIT = 100
# Repo mount path for frontend/clans.html inside the production container — mirrors
# events.py's _EVENTS_HTML_PATH (used by /api/events-embed) and main.py's
# _INDEX_HTML_PATH (used by /api/news-embed). Kept local to this router for the same
# reason events.py keeps its own copy rather than importing main.py's.
_CLANS_HTML_PATH = "/opt/vrising-site/frontend/clans.html"

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
    bases_result = await db.execute(
        select(GameClanBase).where(GameClanBase.clan_id == clan.id)
    )
    base_list = [
        {
            "level": b.level, "floor_count": b.floor_count, "is_raid_protected": b.is_raid_protected,
            "min_x": b.min_x, "min_z": b.min_z, "max_x": b.max_x, "max_z": b.max_z,
        }
        for b in bases_result.scalars().all()
    ]
    base = {
        "id": clan.id, "server_num": clan.server_num, "clan_guid": clan.clan_guid,
        "server_name": server_names.get(clan.server_num) or f"Сервер {clan.server_num}",
        "name": clan.name, "motto": clan.motto or "", "updated_at": clan.updated_at,
        "member_count": member_count, "bases": base_list,
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
                "is_online": m.is_online,
                "last_connected_unix": m.last_connected_unix,
                "physical_power": m.physical_power,
                "spell_power": m.spell_power,
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
            "is_online": m.is_online,
            "last_connected_unix": m.last_connected_unix,
            "physical_power": m.physical_power,
            "spell_power": m.spell_power,
        })

    # Castle base(s) per clan — one bulk query, same pattern as the member preview above.
    all_bases = (await db.execute(
        select(GameClanBase).where(GameClanBase.clan_id.in_(clan_ids))
    )).scalars().all()
    bases_by_clan: dict[int, list[dict]] = {}
    for b in all_bases:
        bases_by_clan.setdefault(b.clan_id, []).append({
            "level": b.level, "floor_count": b.floor_count, "is_raid_protected": b.is_raid_protected,
            "min_x": b.min_x, "min_z": b.min_z, "max_x": b.max_x, "max_z": b.max_z,
        })

    out = [
        {
            "id": c.id, "server_num": c.server_num, "clan_guid": c.clan_guid,
            "server_name": server_names.get(c.server_num) or f"Сервер {c.server_num}",
            "name": c.name, "motto": c.motto or "", "updated_at": c.updated_at,
            "member_count": counts.get(c.id, 0),
            "member_preview": previews_by_clan.get(c.id, []),
            "bases": bases_by_clan.get(c.id, []),
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


@router.get("/api/clans/leaderboard", response_model=list[GameClanLeaderboardOut])
async def clans_leaderboard(server: Optional[int] = None, limit: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    """Ranks clans by total combat power (sum of physical_power+spell_power across the
    clan's FULL member roster — not just the 4-member preview GET /api/clans exposes,
    which would understate/misorder anything bigger than that). member_count and
    online_count ride along as displayed secondary stats, same fields the clan cards
    already show elsewhere on the site.

    NOTE: this must be registered before GET /api/clans/{clan_id} — that route's
    clan_id:int path converter would otherwise 422 on the literal segment "leaderboard"
    before this route ever got a chance to match (FastAPI/Starlette try routes in
    registration order, and a type-conversion failure on a match is not treated as a
    non-match)."""
    limit = _LEADERBOARD_DEFAULT_LIMIT if limit is None else max(1, min(limit, _LEADERBOARD_MAX_LIMIT))

    query = select(GameClan)
    if server is not None:
        query = query.where(GameClan.server_num == server)
    clans = (await db.execute(query)).scalars().all()
    if not clans:
        return []
    clan_ids = [c.id for c in clans]
    server_names = await _get_server_names(db)

    # One grouped query for member_count/online_count/total_power across every clan —
    # same "bulk query, not N+1" pattern GET /api/clans already uses for its own
    # per-clan aggregates. NULL physical_power/spell_power (character never spawned in
    # world yet) count as 0 toward the sum rather than excluding the member.
    agg_rows = (await db.execute(
        select(
            GameClanMember.clan_id,
            func.count(GameClanMember.id),
            func.sum(case((GameClanMember.is_online, 1), else_=0)),
            func.sum(func.coalesce(GameClanMember.physical_power, 0.0) + func.coalesce(GameClanMember.spell_power, 0.0)),
        ).where(GameClanMember.clan_id.in_(clan_ids)).group_by(GameClanMember.clan_id)
    )).all()
    agg_by_clan = {clan_id: (count, online, power) for clan_id, count, online, power in agg_rows}

    out = []
    for c in clans:
        count, online, power = agg_by_clan.get(c.id, (0, 0, 0.0))
        # Same "0-member clans aren't a real community yet" exclusion as GET /api/clans
        # — an abandoned/throwaway clan has no members to rank by power in the first
        # place.
        if count <= 0:
            continue
        out.append({
            "id": c.id, "server_num": c.server_num,
            "server_name": server_names.get(c.server_num) or f"Сервер {c.server_num}",
            "name": c.name, "motto": c.motto or "",
            "member_count": count, "online_count": online or 0,
            "total_power": float(power or 0.0), "avg_power": float(power or 0.0) / count,
        })

    # Primary: total power (the whole point of this endpoint vs. GET /api/clans's
    # member_count sort). Ties broken by member count, then name, for a stable order
    # across requests instead of depending on incidental DB row order.
    out.sort(key=lambda c: (-c["total_power"], -c["member_count"], c["name"]))
    return out[:limit]


@router.get("/api/clans/mine")
async def my_clan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Site user's in-game clan card for the homepage "Твой клан" widget (frontend/
    index.js) — looked up via User.steam_id, the authoritative site-account ↔ game-
    account link set by the plugin's .register/.login commands (see models.py's
    docstring on that column). No linked steam_id and "linked but not currently in any
    GameClanMember roster" both collapse to the same {"clan": None} response — the
    homepage card just hides either way, no need for the visitor to distinguish them.

    Registered above GET /api/clans/{clan_id} for the same int-converter reason
    /api/clans/leaderboard is (see that route's comment) — "mine" would otherwise never
    get a chance to match."""
    if not current_user.steam_id:
        return {"clan": None}
    member = (await db.execute(
        select(GameClanMember).where(GameClanMember.steam_id == current_user.steam_id)
    )).scalars().first()
    if member is None:
        return {"clan": None}
    clan = await db.get(GameClan, member.clan_id)
    if clan is None:
        return {"clan": None}
    agg = (await db.execute(
        select(
            func.count(GameClanMember.id),
            func.sum(case((GameClanMember.is_online, 1), else_=0)),
        ).where(GameClanMember.clan_id == clan.id)
    )).first()
    member_count = (agg[0] if agg else 0) or 0
    online_count = (agg[1] if agg else 0) or 0
    return {"clan": {
        "id": clan.id, "name": clan.name,
        "my_role": member.role,
        "member_count": member_count,
        "online_count": online_count,
    }}


@router.get("/api/clans/{clan_id}", response_model=GameClanDetailOut)
async def get_clan(clan_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(GameClan).where(GameClan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    return await _game_clan_out(db, clan, with_members=True)


@router.get("/api/clans/{clan_id}/history", response_model=list[ClanMembershipEventOut])
async def get_clan_history(clan_id: int, limit: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    """Recent join/leave activity for one clan — see models.py's ClanMembershipEvent for
    how these rows get written (POST /api/plugin/clans/sync diffs the old roster against
    each incoming payload). Looked up by clan_guid/server_num rather than GameClan.id
    since GameClanMembershipEvent has no FK to game_clans (that row's id churns every
    sync cycle — see the model's docstring), so this must resolve today's GameClan.id
    from the request to the stable clan_guid first."""
    clan = (await db.execute(select(GameClan).where(GameClan.id == clan_id))).scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    limit = _HISTORY_DEFAULT_LIMIT if limit is None else max(1, min(limit, _HISTORY_MAX_LIMIT))
    rows = (await db.execute(
        select(ClanMembershipEvent)
        .where(ClanMembershipEvent.clan_guid == clan.clan_guid, ClanMembershipEvent.server_num == clan.server_num)
        .order_by(ClanMembershipEvent.recorded_at.desc(), ClanMembershipEvent.id.desc())
        .limit(limit)
    )).scalars().all()
    return [ClanMembershipEventOut.model_validate(r) for r in rows]


# ─── Link-unfurl embed ───────────────────────────────────────────────────────
# Mirrors GET /api/events-embed in backend/routers/events.py (see that function's own
# comment for the fuller rationale): link-unfurlers (Discord/Telegram/VK/Twitter, most
# search bots) don't run JS, so they never see clans.html's client-side
# openClanDetail()'s meta swap, and a shared clans.html?clan=<id> link would otherwise
# always show the generic clans-list title/description/image no matter which clan it
# was. Re-uses frontend/clans.html itself (read from the repo mount) so layout/styling
# never drifts out of sync — only the meta tag values are swapped before serving.
# Wiring nginx's crawler-UA routing (see events_embed's comment on the same gap) is the
# same out-of-scope follow-up here too — this endpoint is functional and tested
# standalone in the meantime.
_CLANS_EMBED_META_PATTERNS = [
    (re.compile(r'(<title id="page-title">).*?(</title>)'), "title"),
    (re.compile(r'(<meta id="meta-description"[^>]*content=")[^"]*(")'), "desc"),
    (re.compile(r'(<link rel="canonical" href=")[^"]*(")'), "url"),
    (re.compile(r'(<meta property="og:url" content=")[^"]*(")'), "url"),
    (re.compile(r'(<meta id="meta-og-title"[^>]*content=")[^"]*(")'), "title"),
    (re.compile(r'(<meta id="meta-og-description"[^>]*content=")[^"]*(")'), "desc"),
    (re.compile(r'(<meta property="og:image" content=")[^"]*(")'), "image"),
]


@router.get("/api/clans-embed")
async def clans_embed(id: int, db: AsyncSession = Depends(get_db)):
    """Server-rendered <head> meta for one clan, for crawlers that don't run JS. Falls
    back to the page's default meta for an unknown/missing clan id, same as
    events_embed. Uses only fields GET /api/clans/{clan_id} already exposes (name,
    server, member count) plus a total-power figure computed the same way
    GET /api/clans/leaderboard already does — no new clan fields added for this."""
    try:
        with open(_CLANS_HTML_PATH, "r", encoding="utf-8") as f:
            page = f.read()
    except OSError as e:
        raise HTTPException(status_code=404, detail="clans.html not found") from e

    clan = (await db.execute(select(GameClan).where(GameClan.id == id))).scalar_one_or_none()
    if clan is None:
        return Response(content=page, media_type="text/html; charset=utf-8")

    base_url = "https://v.just-skill.ru"
    try:
        su_res = await db.execute(select(Setting).where(Setting.key == "https_domain"))
        su = su_res.scalar_one_or_none()
        if su and su.value.strip():
            base_url = f"https://{su.value.strip()}"
    except Exception:
        pass

    server_names = await _get_server_names(db)
    server_name = server_names.get(clan.server_num) or f"Сервер {clan.server_num}"
    agg = (await db.execute(
        select(
            func.count(GameClanMember.id),
            func.sum(func.coalesce(GameClanMember.physical_power, 0.0) + func.coalesce(GameClanMember.spell_power, 0.0)),
        ).where(GameClanMember.clan_id == clan.id)
    )).first()
    member_count = (agg[0] if agg else 0) or 0
    total_power = float((agg[1] if agg else 0.0) or 0.0)

    image = f"{base_url}/uploads/og-default.png"
    link = f"{base_url}/clans.html?clan={clan.id}"
    desc_bits = [server_name, f"участников: {member_count}"]
    if total_power > 0:
        desc_bits.append(f"мощь: {int(total_power)}")
    plain_desc = ", ".join(desc_bits)

    values = {
        "title": html.escape(f"{clan.name} — V Rising"),
        "desc": html.escape(plain_desc),
        "url": html.escape(link),
        "image": html.escape(image),
    }
    for pattern, key in _CLANS_EMBED_META_PATTERNS:
        page = pattern.sub(lambda m, v=values[key]: m.group(1) + v + m.group(2), page, count=1)

    return Response(content=page, media_type="text/html; charset=utf-8")
