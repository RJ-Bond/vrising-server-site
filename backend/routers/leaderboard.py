from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from ..database import get_db
from ..models import User, PlayerRecord, PlayerRankSnapshot
from ..auth import get_admin_user
from ..schemas import PlayerRecordOut, PointsLeaderboardEntryOut

router = APIRouter()


# ─── Leaderboard ─────────────────────────────────────────────────────────────

@router.get("/api/leaderboard", response_model=list[PlayerRecordOut])
async def get_leaderboard(
    server: int = Query(1),
    period: str = Query("all"),
    q: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(PlayerRecord).where(PlayerRecord.server_num == server)
    if period in ("week", "month"):
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff -= timedelta(days=7 if period == "week" else 30)
        query = query.where(PlayerRecord.last_seen >= cutoff)
    if q.strip():
        query = query.where(PlayerRecord.player_name.ilike(f"%{q.strip()}%"))
    query = query.order_by(PlayerRecord.total_seconds.desc()).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    records = result.scalars().all()

    avatar_map = {}
    if records:
        names = [r.player_name for r in records]
        users_result = await db.execute(select(User.username, User.avatar_url).where(User.username.in_(names)))
        avatar_map = {u.username: u.avatar_url for u in users_result.all()}

    hist_rank_map = {}
    if period == "all" and records:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        sub = (
            select(PlayerRankSnapshot.player_name, func.max(PlayerRankSnapshot.recorded_at).label("max_ts"))
            .where(PlayerRankSnapshot.server_num == server, PlayerRankSnapshot.recorded_at <= cutoff)
            .group_by(PlayerRankSnapshot.player_name)
            .subquery()
        )
        hist_result = await db.execute(
            select(PlayerRankSnapshot.player_name, PlayerRankSnapshot.total_seconds)
            .join(sub, and_(
                PlayerRankSnapshot.player_name == sub.c.player_name,
                PlayerRankSnapshot.recorded_at == sub.c.max_ts,
            ))
            .where(PlayerRankSnapshot.server_num == server)
        )
        hist_rows = hist_result.all()
        for rank_then, (name, _secs) in enumerate(sorted(hist_rows, key=lambda row: row.total_seconds, reverse=True), start=1):
            hist_rank_map[name] = rank_then

    out = []
    for i, r in enumerate(records):
        item = PlayerRecordOut.model_validate(r)
        item.avatar_url = avatar_map.get(r.player_name)
        item.verified = r.steam_id is not None
        if period == "all" and r.player_name in hist_rank_map:
            current_rank = (page - 1) * per_page + i + 1
            item.rank_delta = hist_rank_map[r.player_name] - current_rank
        out.append(item)
    return out


@router.get("/api/leaderboard/points", response_model=list[PointsLeaderboardEntryOut])
async def get_points_leaderboard(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Points-economy leaderboard: site accounts ranked by points_balance (earned via
    playtime/streak, spent in the shop — see _award_points()) descending. Unlike the
    playtime leaderboard above this is not per-server (points_balance is a single global
    balance per User) and has no week/month period filter (it's a running balance, not a
    time-bucketed stat). Zero/negative balances and deactivated accounts are excluded,
    same spirit as the playtime leaderboard only ever having rows for players who've
    actually accrued something."""
    query = (
        select(User)
        .where(User.is_active == True, User.points_balance > 0)
        .order_by(User.points_balance.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(query)
    users = result.scalars().all()
    return [PointsLeaderboardEntryOut.model_validate(u) for u in users]


@router.delete("/api/admin/leaderboard/{record_id}", status_code=204)
async def delete_leaderboard_record(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(PlayerRecord).where(PlayerRecord.id == record_id))
    rec = result.scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="Record not found")
    await db.delete(rec)
    await db.commit()
