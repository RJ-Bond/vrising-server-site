"""GET /api/activity-feed — a small, read-only "what's happening right now" feed for
the homepage. Exists as a same-site alternative to routing community engagement
through Discord/Telegram (both restricted for this site's audience) — NOT a chat
system: no composition, no threading, no moderation queue, just a reverse-chronological
merge of notable events that already happened elsewhere on the site:

  - Recently published news
  - Recently created events (frontend/events.html has no per-event deep link yet, so
    items point at the events list page, not a specific event)
  - Players who just crossed a playtime-hours or connect-streak milestone
  - Recently fulfilled points-shop redemptions (item name + who redeemed it)

Deliberately builds on data that already exists (News/Event/PlayerRecord/
PlayerRankSnapshot/PlayerDailyActivity) — no new tracking table. Clan changes are
NOT included: POST /api/plugin/clans/sync (backend/routers/plugin_integration.py)
deletes and fully re-inserts every GameClan row for a server on every sync cycle, so
GameClan.id/created_at/updated_at reset on every cycle and never reflect an actual
in-game change — there is nothing meaningful to diff without a new tracking table,
which is explicitly out of scope here.

Public, unauthenticated — same visibility as the news feed / leaderboard / events
list this merges together, all of which are already public. Capped and cheap since
this is expected to load on every homepage visit: fixed per-category caps, no N+1
per-row queries (bulk lookups only, same style as GET /api/leaderboard).
"""
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..helpers import _fmt_dt, _site_timezone
from ..models import Event, News, PlayerDailyActivity, PlayerRankSnapshot, PlayerRecord, ShopRedemption, User

router = APIRouter()

FEED_LIMIT = 20
_NEWS_LIMIT = 6
_EVENTS_LIMIT = 6
_MILESTONE_LIMIT = 8
_SHOP_LIMIT = 6
# How far back a player must have been seen to even be considered for a milestone
# crossing — bounds the pool query and keeps "just crossed" meaning "recently", not
# "at some point in this account's history".
_MILESTONE_LOOKBACK_DAYS = 3
_MILESTONE_POOL_CAP = 200

# Same thresholds/icons/labels as frontend/leaderboard.html's achievementBadge()
# (36000/360000/3600000 seconds = 10/100/1000 hours) — kept in sync manually, same as
# that function's own comment says it mirrors user.html's PUB_ACHIEVEMENTS tiers.
# Ordered highest-first so a player who jumped straight past a lower tier (e.g. a big
# offline playtime import) is credited with the highest tier actually crossed.
_HOUR_TIERS: list[tuple[int, str, str]] = [
    (3600000, "1000+ часов на сервере — легенда сервера", "🏆"),
    (360000, "100+ часов на сервере", "⚔"),
    (36000, "10+ часов на сервере", "⏱"),
]

# frontend/leaderboard.html's streakBadge() shows the raw current streak with no
# tiers of its own (it's a continuous counter, not a badge with levels), so unlike
# the hour tiers above there's no existing threshold set to mirror. These are picked
# as reasonable "worth celebrating" round numbers for the feed specifically.
_STREAK_MILESTONE_DAYS = (3, 7, 14, 30, 60, 100)


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian pluralization — same algorithm as pluralRu() duplicated across
    frontend/leaderboard.html and frontend/clans.html; ported here since this feed's
    streak-milestone text is generated server-side."""
    mod10, mod100 = n % 10, n % 100
    if mod10 == 1 and mod100 != 11:
        return one
    if 2 <= mod10 <= 4 and (mod100 < 10 or mod100 >= 20):
        return few
    return many


def _current_streak(activity_dates: set[str], today: date) -> int:
    """Consecutive calendar days ending today (or yesterday, grace period for a player
    who hasn't connected yet today) — identical logic to
    backend/routers/leaderboard.py's _current_streak(); duplicated rather than
    cross-imported since routers in this codebase don't import each other (each stays
    self-contained), see that function's docstring for the full reasoning."""
    cursor = today if today.isoformat() in activity_dates else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in activity_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


class ActivityFeedItemOut(BaseModel):
    type: Literal["news", "event", "milestone", "shop_redemption"]
    title: str
    subtitle: str = ""
    url: str
    icon: str = ""
    timestamp: Optional[str] = None


@router.get("/api/activity-feed", response_model=list[ActivityFeedItemOut])
async def get_activity_feed(
    server: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    raw_items: list[tuple[datetime, ActivityFeedItemOut]] = []

    # ─── Recent news ─────────────────────────────────────────────────────────
    news_rows = (await db.execute(
        select(News.title, News.slug, News.summary, News.created_at)
        .where(News.published == True)  # noqa: E712 — SQLAlchemy idiom, see pyproject.toml
        .order_by(News.created_at.desc())
        .limit(_NEWS_LIMIT)
    )).all()
    for title, slug, summary, created_at in news_rows:
        raw_items.append((created_at, ActivityFeedItemOut(
            type="news", title=title, subtitle=(summary or "").strip(),
            url=f"/?news={slug}", icon="📰", timestamp=_fmt_dt(created_at),
        )))

    # ─── Recently created events ─────────────────────────────────────────────
    event_rows = (await db.execute(
        select(Event.title, Event.start_date, Event.created_at)
        .order_by(Event.created_at.desc())
        .limit(_EVENTS_LIMIT)
    )).all()
    for title, start_date, created_at in event_rows:
        raw_items.append((created_at, ActivityFeedItemOut(
            type="event", title=title,
            subtitle=f"Начало: {_fmt_dt(start_date)}" if start_date else "",
            url="/events.html", icon="📅", timestamp=_fmt_dt(created_at),
        )))

    # ─── Recent shop purchases ────────────────────────────────────────────────
    # Fulfilled ShopRedemption rows only (not "pending"/"cancelled" — a pending request
    # isn't a real event yet, and this is simpler than a standalone widget since the
    # feed UI already exists on the homepage). item_name_snapshot/resolved_by are copied
    # at purchase/fulfil time (see ShopRedemption's docstring in models.py) so this never
    # needs to join the (possibly since-deleted) ShopItem row.
    shop_rows = (await db.execute(
        select(ShopRedemption.item_name_snapshot, ShopRedemption.resolved_at, ShopRedemption.created_at, User.username)
        .join(User, User.id == ShopRedemption.user_id)
        .where(ShopRedemption.status == "fulfilled")
        .order_by(ShopRedemption.resolved_at.desc())
        .limit(_SHOP_LIMIT)
    )).all()
    for item_name, resolved_at, created_at, username in shop_rows:
        ts = resolved_at or created_at
        raw_items.append((ts, ActivityFeedItemOut(
            type="shop_redemption", title=item_name, subtitle=f"Получил: {username}",
            url="/shop.html", icon="🛒", timestamp=_fmt_dt(ts),
        )))

    # ─── Playtime-hours / connect-streak milestones ─────────────────────────
    # Pool: verified (steam_id-linked) players seen recently — bounds both queries
    # below to a small, recently-relevant set instead of scanning every player ever
    # recorded, and "recently seen" doubles as the "just crossed" recency signal for
    # the crossing detected within it.
    lookback_cutoff = datetime.now(timezone.utc) - timedelta(days=_MILESTONE_LOOKBACK_DAYS)
    pool_rows = (await db.execute(
        select(PlayerRecord.player_name, PlayerRecord.steam_id, PlayerRecord.total_seconds, PlayerRecord.last_seen)
        .where(
            PlayerRecord.server_num == server,
            PlayerRecord.steam_id != None,  # noqa: E711 — SQLAlchemy idiom, see pyproject.toml
            PlayerRecord.last_seen != None,  # noqa: E711
            PlayerRecord.last_seen >= lookback_cutoff.replace(tzinfo=None),
        )
        .order_by(PlayerRecord.last_seen.desc())
        .limit(_MILESTONE_POOL_CAP)
    )).all()

    if pool_rows:
        steam_ids = [p.steam_id for p in pool_rows]
        player_names = [p.player_name for p in pool_rows]

        # Site accounts linked to these steam IDs — steam_id (set via the in-game
        # .register/.login commands) is the authoritative site-account<->game-account
        # link (see the comment on User.steam_id in models.py), used here instead of
        # the username==player_name string match GET /api/leaderboard's avatar_map
        # uses, since a milestone item needs a real profile link, not just an avatar.
        user_rows = (await db.execute(
            select(User.steam_id, User.username, User.avatar_url).where(User.steam_id.in_(steam_ids))
        )).all()
        user_by_steam = {u.steam_id: u for u in user_rows}

        # Baseline total_seconds as of each player's most recent PlayerRankSnapshot
        # (nightly snapshots, same table GET /api/leaderboard's rank-delta feature
        # reads) — comparing current total_seconds against this tells us whether a
        # tier was crossed recently, not just currently exceeded. Players with no
        # snapshot yet (brand new) are skipped rather than treated as a baseline of
        # 0 — that would falsely credit "just crossed 10h" to anyone whose very first
        # session happened to be a long one.
        snap_sub = (
            select(PlayerRankSnapshot.player_name, func.max(PlayerRankSnapshot.recorded_at).label("max_ts"))
            .where(PlayerRankSnapshot.server_num == server, PlayerRankSnapshot.player_name.in_(player_names))
            .group_by(PlayerRankSnapshot.player_name)
            .subquery()
        )
        snap_rows = (await db.execute(
            select(PlayerRankSnapshot.player_name, PlayerRankSnapshot.total_seconds)
            .join(snap_sub, and_(
                PlayerRankSnapshot.player_name == snap_sub.c.player_name,
                PlayerRankSnapshot.recorded_at == snap_sub.c.max_ts,
            ))
            .where(PlayerRankSnapshot.server_num == server)
        )).all()
        prev_totals = {name: secs for name, secs in snap_rows}

        # Current connect-streak per steam_id — same bulk-fetch-then-compute-in-memory
        # approach as GET /api/leaderboard (see that router's matching block).
        today = datetime.now(await _site_timezone(db)).date()
        activity_cutoff = (today - timedelta(days=max(_STREAK_MILESTONE_DAYS) + 1)).isoformat()
        activity_rows = (await db.execute(
            select(PlayerDailyActivity.steam_id, PlayerDailyActivity.activity_date)
            .where(
                PlayerDailyActivity.server_num == server,
                PlayerDailyActivity.steam_id.in_(steam_ids),
                PlayerDailyActivity.activity_date >= activity_cutoff,
            )
        )).all()
        dates_by_steam: dict[str, set[str]] = {}
        for sid, activity_date in activity_rows:
            dates_by_steam.setdefault(sid, set()).add(activity_date)

        milestone_candidates: list[tuple[datetime, ActivityFeedItemOut]] = []
        seen_steam_ids: set[str] = set()
        for p in pool_rows:
            # A renamed character can leave more than one PlayerRecord row behind for
            # the same steam_id (see the uq_player_server constraint's docstring) —
            # only the most-recently-seen row (pool_rows is already last_seen desc)
            # should be able to trigger a milestone for that player this request.
            if p.steam_id in seen_steam_ids:
                continue
            seen_steam_ids.add(p.steam_id)
            user = user_by_steam.get(p.steam_id)
            if user is None:
                continue  # no linked profile to send the feed item to — skip
            profile_url = f"/user.html?u={user.username}"

            prev_total = prev_totals.get(p.player_name)
            if prev_total is not None:
                for threshold, label, icon in _HOUR_TIERS:
                    if prev_total < threshold <= p.total_seconds:
                        milestone_candidates.append((p.last_seen, ActivityFeedItemOut(
                            type="milestone", title=user.username, subtitle=label,
                            url=profile_url, icon=icon, timestamp=_fmt_dt(p.last_seen),
                        )))
                        break

            streak = _current_streak(dates_by_steam.get(p.steam_id, set()), today)
            if streak in _STREAK_MILESTONE_DAYS:
                day_word = _plural_ru(streak, "день", "дня", "дней")
                milestone_candidates.append((p.last_seen, ActivityFeedItemOut(
                    type="milestone", title=user.username,
                    subtitle=f"Играет {streak} {day_word} подряд",
                    url=profile_url, icon="🔥", timestamp=_fmt_dt(p.last_seen),
                )))

        milestone_candidates.sort(key=lambda pair: pair[0], reverse=True)
        raw_items.extend(milestone_candidates[:_MILESTONE_LIMIT])

    raw_items.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in raw_items[:FEED_LIMIT]]
