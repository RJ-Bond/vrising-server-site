from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import Optional

from ..database import get_db
from ..models import User, PlayerRecord, PlayerRankSnapshot, GameClan, GameClanMember, PlayerDailyActivity
from ..auth import get_admin_user
from ..helpers import _site_timezone
from ..schemas import PlayerRecordOut, PointsLeaderboardEntryOut

router = APIRouter()


async def _closest_prior_snapshot_totals(db: AsyncSession, server: int, cutoff: datetime) -> dict[str, int]:
    """Each player's total_seconds from their most recent PlayerRankSnapshot at or
    before `cutoff` — the "closest prior data point" logic shared by the rank-delta
    indicator below (cutoff ~7 days ago) and by GET /api/leaderboard's `as_of` mode
    (cutoff = end of the requested day), so a snapshot interval that doesn't land
    exactly on the requested boundary still resolves to the nearest earlier one
    instead of nothing."""
    sub = (
        select(PlayerRankSnapshot.player_name, func.max(PlayerRankSnapshot.recorded_at).label("max_ts"))
        .where(PlayerRankSnapshot.server_num == server, PlayerRankSnapshot.recorded_at <= cutoff)
        .group_by(PlayerRankSnapshot.player_name)
        .subquery()
    )
    rows = (await db.execute(
        select(PlayerRankSnapshot.player_name, PlayerRankSnapshot.total_seconds)
        .join(sub, and_(
            PlayerRankSnapshot.player_name == sub.c.player_name,
            PlayerRankSnapshot.recorded_at == sub.c.max_ts,
        ))
        .where(PlayerRankSnapshot.server_num == server)
    )).all()
    return {name: total for name, total in rows}


def _current_streak(activity_dates: set[str], today: date) -> int:
    """Consecutive calendar days (site-local, matching PlayerDailyActivity's own
    convention) ending today — or yesterday if the player hasn't connected yet today,
    so the streak doesn't drop to 0 the moment the clock rolls over before they've had
    a chance to play. Mirrors the in-game "you've played N days in a row!" message
    (POST /api/plugin/connect-streak) so the number shown on the site matches what the
    plugin already tells the player, instead of inventing a second definition."""
    cursor = today if today.isoformat() in activity_dates else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in activity_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# ─── Leaderboard ─────────────────────────────────────────────────────────────

@router.get("/api/leaderboard", response_model=list[PlayerRecordOut])
async def get_leaderboard(
    response: Response,
    server: int = Query(1),
    period: str = Query("all"),
    q: str = Query(""),
    clan_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    as_of: Optional[date] = Query(
        None,
        description="Reconstruct the leaderboard as it stood at/before this date from "
        "PlayerRankSnapshot instead of live PlayerRecord totals — each player's closest "
        "prior snapshot is used when there's no exact snapshot for the date (see "
        "_closest_prior_snapshot_totals). Ignores `period`: snapshots only ever store the "
        "cumulative all-time total, so there's no historical day/week/month window to "
        "slice. A date with no snapshot at or before it (older than any recorded data) "
        "returns an empty list rather than an error. Omitting this param leaves today's "
        "live behavior byte-for-byte unchanged.",
    ),
    db: AsyncSession = Depends(get_db),
):
    # Already paginated (page/per_page above); this is the second half of "stays fast as
    # the player count grows" — a short shared cache so a burst of visitors loading the
    # same page hits the DB once instead of once each. Short enough that online-status/
    # rank-delta/streak fields (each with their own real-time-ish freshness expectations
    # elsewhere on the site) don't go noticeably stale — same TTL ballpark as
    # STATUS_CACHE_TTL in main.py, which caches similarly "live-ish" server-status data.
    response.headers["Cache-Control"] = "public, max-age=30"
    if as_of is not None:
        # ── Historical reconstruction from PlayerRankSnapshot ──────────────────────
        cutoff = datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)
        totals = await _closest_prior_snapshot_totals(db, server, cutoff)
        names = list(totals.keys())
        if q.strip():
            qlow = q.strip().lower()
            names = [n for n in names if qlow in n.lower()]

        # Pull whatever current PlayerRecord metadata (id/steam_id/last_seen/session
        # stats) still exists for these names — a player who has since been wiped/
        # deleted from PlayerRecord still shows up in the historical view (they were
        # there on that date), just without that extra metadata.
        pr_map: dict[str, PlayerRecord] = {}
        if names:
            pr_rows = (await db.execute(
                select(PlayerRecord).where(PlayerRecord.server_num == server, PlayerRecord.player_name.in_(names))
            )).scalars().all()
            pr_map = {r.player_name: r for r in pr_rows}

        if clan_id is not None:
            member_steam_ids = set((await db.execute(
                select(GameClanMember.steam_id).where(GameClanMember.clan_id == clan_id)
            )).scalars().all())
            names = [n for n in names if pr_map.get(n) and pr_map[n].steam_id in member_steam_ids]

        names.sort(key=lambda n: totals[n], reverse=True)
        page_names = names[(page - 1) * per_page: (page - 1) * per_page + per_page]
        records = [
            SimpleNamespace(
                id=(pr_map[n].id if n in pr_map else 0),
                server_num=server,
                player_name=n,
                total_seconds=totals[n],
                last_seen=(pr_map[n].last_seen if n in pr_map else None),
                last_duration=(pr_map[n].last_duration if n in pr_map else 0),
                session_count=(pr_map[n].session_count if n in pr_map else 0),
                steam_id=(pr_map[n].steam_id if n in pr_map else None),
            )
            for n in page_names
        ]
    else:
        # ── Live leaderboard — unchanged from before `as_of` existed ───────────────
        query = select(PlayerRecord).where(PlayerRecord.server_num == server)
        if period in ("day", "week", "month"):
            cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff -= timedelta(days=1 if period == "day" else 7 if period == "week" else 30)
            query = query.where(PlayerRecord.last_seen >= cutoff)
        if q.strip():
            query = query.where(PlayerRecord.player_name.ilike(f"%{q.strip()}%"))
        if clan_id is not None:
            member_steam_ids = (await db.execute(
                select(GameClanMember.steam_id).where(GameClanMember.clan_id == clan_id)
            )).scalars().all()
            # No members (or an unknown clan_id) must mean "zero results", not "unfiltered" —
            # an empty IN-list would otherwise match nothing anyway, but PlayerRecord.steam_id
            # is nullable (A2S-only rows), so being explicit here avoids relying on that.
            query = query.where(PlayerRecord.steam_id.in_(member_steam_ids) if member_steam_ids else False)
        query = query.order_by(PlayerRecord.total_seconds.desc()).offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        records = result.scalars().all()

    avatar_map = {}
    if records:
        names = [r.player_name for r in records]
        users_result = await db.execute(select(User.username, User.avatar_url).where(User.username.in_(names)))
        avatar_map = {u.username: u.avatar_url for u in users_result.all()}

    # Combat power / clan / online status — all reported by the game plugin's clan-sync
    # cycle (POST /api/plugin/clans/sync) into GameClanMember, keyed by steam_id. Only
    # ever populated for players currently in a synced clan; None/absent for everyone
    # else rather than a misleading 0 or "offline".
    clan_map = {}
    steam_ids = [r.steam_id for r in records if r.steam_id]
    if steam_ids:
        clan_rows = (await db.execute(
            select(
                GameClanMember.steam_id, GameClanMember.physical_power, GameClanMember.spell_power,
                GameClanMember.is_online, GameClan.id, GameClan.name,
            ).join(GameClan, GameClan.id == GameClanMember.clan_id)
            .where(GameClanMember.steam_id.in_(steam_ids))
        )).all()
        clan_map = {
            row.steam_id: {
                "physical_power": row.physical_power, "spell_power": row.spell_power,
                "is_online": row.is_online, "clan_id": row.id, "clan_name": row.name,
            }
            for row in clan_rows
        }

    # Current connect-streak (see _current_streak's docstring) — scoped to this server,
    # same as PlayerDailyActivity recording (server_num, steam_id, activity_date).
    streak_map = {}
    if steam_ids:
        today = datetime.now(await _site_timezone(db)).date()
        cutoff_date = (today - timedelta(days=60)).isoformat()  # streaks longer than this are not realistic to expect and would just cost more rows scanned
        activity_rows = (await db.execute(
            select(PlayerDailyActivity.steam_id, PlayerDailyActivity.activity_date)
            .where(
                PlayerDailyActivity.server_num == server,
                PlayerDailyActivity.steam_id.in_(steam_ids),
                PlayerDailyActivity.activity_date >= cutoff_date,
            )
        )).all()
        by_steam_id: dict[str, set[str]] = {}
        for row in activity_rows:
            by_steam_id.setdefault(row.steam_id, set()).add(row.activity_date)
        streak_map = {sid: _current_streak(dates, today) for sid, dates in by_steam_id.items()}

    # Rank-delta-vs-~7-days-ago indicator — meaningless (and skipped) in `as_of` mode:
    # the point of that mode is a fixed historical view, not "climbed/dropped since",
    # and computing a delta relative to *today's* live standings while showing a past
    # date's totals would be misleading.
    hist_rank_map = {}
    if as_of is None and period == "all" and records:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        totals_7d_ago = await _closest_prior_snapshot_totals(db, server, cutoff)
        for rank_then, (name, _secs) in enumerate(sorted(totals_7d_ago.items(), key=lambda kv: kv[1], reverse=True), start=1):
            hist_rank_map[name] = rank_then

    out = []
    for i, r in enumerate(records):
        item = PlayerRecordOut.model_validate(r)
        item.avatar_url = avatar_map.get(r.player_name)
        item.verified = r.steam_id is not None
        if as_of is None and period == "all" and r.player_name in hist_rank_map:
            current_rank = (page - 1) * per_page + i + 1
            item.rank_delta = hist_rank_map[r.player_name] - current_rank
        clan_info = clan_map.get(r.steam_id) if r.steam_id else None
        if clan_info:
            item.clan_id = clan_info["clan_id"]
            item.clan_name = clan_info["clan_name"]
            item.physical_power = clan_info["physical_power"]
            item.spell_power = clan_info["spell_power"]
            item.is_online = clan_info["is_online"]
        item.streak_days = streak_map.get(r.steam_id, 0) if r.steam_id else 0
        out.append(item)
    return out


@router.get("/api/leaderboard/snapshot-range")
async def get_leaderboard_snapshot_range(server: int = Query(1), db: AsyncSession = Depends(get_db)):
    """Earliest PlayerRankSnapshot date recorded for this server — lets the frontend's
    `as_of` date picker (GET /api/leaderboard?as_of=YYYY-MM-DD) disable/reject dates
    with no data behind them instead of silently rendering an empty leaderboard.
    {"earliest_date": null} means the nightly snapshot task hasn't run yet at all for
    this server (day-1-of-the-feature state, same case test_rank_delta_before_any_
    snapshot_exists covers for the rank-delta indicator)."""
    earliest = (await db.execute(
        select(func.min(PlayerRankSnapshot.recorded_at)).where(PlayerRankSnapshot.server_num == server)
    )).scalar_one_or_none()
    return {"earliest_date": earliest.date().isoformat() if earliest else None}


@router.get("/api/leaderboard/trend")
async def get_leaderboard_trend(
    player_name: str = Query(...),
    server: int = Query(1),
    days: int = Query(14, ge=7, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Daily playtime for one leaderboard row's expand-to-see-trend UI — same diff-of-
    cumulative-PlayerRankSnapshot approach as GET /api/users/{username}/activity-trend
    (backend/routers/users.py), but keyed by player_name directly instead of a linked
    site account, since most leaderboard rows aren't linked to one. Deliberately public
    (no auth) — the same total_seconds this derives from is already public via the
    leaderboard itself, so a daily breakdown of it isn't a new information disclosure."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days + 1)
    rows = (await db.execute(
        select(
            func.date(PlayerRankSnapshot.recorded_at).label("day"),
            PlayerRankSnapshot.total_seconds,
        )
        .where(
            PlayerRankSnapshot.player_name == player_name,
            PlayerRankSnapshot.server_num == server,
            PlayerRankSnapshot.recorded_at >= cutoff,
        )
        .order_by(PlayerRankSnapshot.recorded_at.asc())
    )).all()
    trend = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        delta = max(0, cur.total_seconds - prev.total_seconds)
        trend.append({"date": cur.day, "seconds": delta})
    return trend


@router.get("/api/leaderboard/points", response_model=list[PointsLeaderboardEntryOut])
async def get_points_leaderboard(
    response: Response,
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
    response.headers["Cache-Control"] = "public, max-age=30"  # same short shared cache as GET /api/leaderboard above
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
