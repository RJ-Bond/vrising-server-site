import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, or_

from ..database import get_db
from ..models import (
    User, Setting, PlayerRecord, PluginHeartbeat, GameClan, GameClanMember,
    Announcement, ServerMessageTemplate, ScheduledRestart, PlayerDailyActivity,
)
from ..auth import get_password_hash, verify_password
from ..rate_limit import limiter
from ..helpers import (
    _require_plugin_key,
    _site_timezone,
    _get_points_config,
    _award_points,
    _fmt_dt_z,
    _schedule_restart,
    _cancel_restart,
)
from ..schemas import (
    PluginAcceptRules,
    PluginRegister,
    PluginLogin,
    PluginHeartbeatIn,
    PluginSessionReport,
    PluginClansSyncIn,
    ServerMessageTemplateOut,
    PluginScheduleRestartIn,
    PluginCancelRestartIn,
    PluginConnectStreakIn,
)

router = APIRouter()


# ─── Game Plugin Integration ──────────────────────────────────────────────────
# Server-to-site channel used by the BepInEx plugin (vrising-bepinex-plugin) for the
# in-game .register/.login chat commands. Authenticated via a shared secret sent as the
# X-Plugin-Key header — this is the game server itself calling as a trusted caller, not
# an individual player, so it does not go through the user JWT scheme at all.
#
# Each server_num may optionally have its own key (ServerApiKey table, managed via
# GET/PUT /api/admin/server-api-key below) for better isolation — a leaked config only
# compromises the one server. Servers without a row there fall back to the single
# global secret (Setting "plugin_api_key", set in admin settings), preserving backward
# compatibility with already-deployed plugin configs that only know the shared key.

@router.get("/api/plugin/status")
@limiter.limit("60/minute")
async def plugin_status(
    request: Request,
    steam_id: str,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Checked by the plugin on player connect to decide whether to show the
    "you're not registered yet" chat message (or, if already linked, a
    "logged in as {username}" welcome message)."""
    result = await db.execute(
        select(User.username, User.rules_accepted_at).where(User.steam_id == steam_id)
    )
    row = result.first()
    if row is None:
        return {"registered": False, "username": None, "rules_accepted": None}
    username, rules_accepted_at = row
    return {
        "registered": True,
        "username": username,
        "rules_accepted": rules_accepted_at is not None,
    }


@router.get("/api/plugin/rules")
@limiter.limit("60/minute")
async def plugin_get_rules(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Returns the server rules (site admin-managed, Setting "rules") so the plugin can
    show them to a freshly-registered player and prompt for in-game acceptance. Rules
    aren't actually per-server, but server_num is still accepted/required like the other
    plugin GET endpoints for consistent API-key resolution in _require_plugin_key."""
    result = await db.execute(select(Setting).where(Setting.key == "rules"))
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return {"rules": []}
    try:
        rules = json.loads(setting.value)
    except (TypeError, ValueError):
        rules = []
    return {"rules": rules}


@router.post("/api/plugin/accept-rules")
@limiter.limit("30/minute")
async def plugin_accept_rules(
    request: Request,
    body: PluginAcceptRules,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game rules-acceptance prompt — mirrors POST /api/auth/accept-rules
    for website users, but keyed by steam_id since there's no JWT session in-game."""
    result = await db.execute(select(User).where(User.steam_id == body.steam_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="not_registered")
    if user.rules_accepted_at is None:
        user.rules_accepted_at = datetime.now(timezone.utc)
        await db.commit()
    return {"success": True}


@router.post("/api/plugin/register")
@limiter.limit("10/minute")
async def plugin_register(
    request: Request,
    body: PluginRegister,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .register <password> command. Username is the game character
    name (not a separate field the player types); email is a synthesized placeholder
    since chat commands have nowhere to collect a real one — the player can set a real
    email later from their site profile."""
    existing_steam = await db.execute(select(User.id).where(User.steam_id == body.steam_id))
    if existing_steam.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="already_registered")

    username = body.character_name.strip()[:32]
    if not re.match(r"^[a-zA-Z0-9_а-яёА-ЯЁ ]{3,32}$", username):
        raise HTTPException(status_code=400, detail="invalid_username")

    existing_username = await db.execute(select(User.id).where(User.username == username))
    if existing_username.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="username_taken")

    user = User(
        username=username,
        email=f"steam_{body.steam_id}@vrising.local",
        hashed_password=get_password_hash(body.password),
        role="user",
        game_nickname=username,
        steam_id=body.steam_id,
    )
    db.add(user)
    await db.commit()
    return {"success": True, "username": username}


@router.post("/api/plugin/login")
@limiter.limit("10/minute")
async def plugin_login(
    request: Request,
    body: PluginLogin,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .login <password> command — links steam_id to an existing
    site account (e.g. one created via the website) after verifying credentials, for
    players who already have a site account under their character name."""
    username = body.character_name.strip()[:32]
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="bad_credentials")
    if user.steam_id and user.steam_id != body.steam_id:
        raise HTTPException(status_code=409, detail="linked_elsewhere")
    if user.steam_id != body.steam_id:
        user.steam_id = body.steam_id
        await db.commit()
    return {"success": True, "username": user.username}


@router.post("/api/plugin/heartbeat")
@limiter.limit("120/minute")
async def plugin_heartbeat(
    request: Request,
    body: PluginHeartbeatIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Sent periodically (~every 60s) by the plugin so the admin panel can show a
    connection-status pill (see GET /api/admin/plugin-status). Upserts the single
    row for this server_num — no history is kept, just the latest snapshot."""
    result = await db.execute(select(PluginHeartbeat).where(PluginHeartbeat.server_num == body.server_num))
    hb = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if hb is None:
        hb = PluginHeartbeat(server_num=body.server_num)
        db.add(hb)
    hb.server_name = body.server_name
    hb.plugin_version = body.plugin_version
    hb.player_count = body.player_count
    hb.last_seen_at = now
    await db.commit()
    return {"success": True}


@router.post("/api/plugin/sessions")
@limiter.limit("60/minute")
async def plugin_report_session(
    request: Request,
    body: PluginSessionReport,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Sent by the plugin's SessionTracker on player disconnect, reporting the session
    that just ended. This is the accuracy source for playtime, replacing A2S polling
    wherever the plugin is running (monitor.py's polling keeps running as a fallback
    for servers without the plugin). Upsert/merge logic, in order:
      1. Match an existing PlayerRecord by (server_num, steam_id) — the stable identity
         once a row has been claimed by a previous report.
      2. Else match an existing A2S-only PlayerRecord by (server_num, player_name,
         steam_id IS NULL) — a row accumulated purely from passive polling that has
         never been claimed by a verified session report yet. Claim it (set steam_id)
         instead of creating a duplicate, preserving its prior total_seconds.
      3. Else create a brand new PlayerRecord for this steam_id.
    In all cases, player_name is refreshed (character rename), and the session's
    seconds/last_seen/session_count are applied on top."""
    result = await db.execute(
        select(PlayerRecord).where(
            PlayerRecord.server_num == body.server_num,
            PlayerRecord.steam_id == body.steam_id,
        )
    )
    record = result.scalar_one_or_none()

    if record is None:
        claim_result = await db.execute(
            select(PlayerRecord).where(
                PlayerRecord.server_num == body.server_num,
                PlayerRecord.player_name == body.character_name,
                PlayerRecord.steam_id.is_(None),
            )
        )
        record = claim_result.scalar_one_or_none()
        if record is not None:
            record.steam_id = body.steam_id

    if record is None:
        record = PlayerRecord(
            server_num=body.server_num,
            player_name=body.character_name,
            steam_id=body.steam_id,
            total_seconds=0,
            session_count=0,
        )
        db.add(record)

    if body.ended_at is not None:
        ended_at = body.ended_at
        if ended_at.tzinfo is not None:
            ended_at = ended_at.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        ended_at = datetime.now(timezone.utc).replace(tzinfo=None)

    record.player_name = body.character_name
    record.total_seconds += body.session_seconds
    record.last_duration = body.session_seconds
    record.last_seen = ended_at
    record.session_count += 1

    # Playtime-earning hook — awards points to the linked site account, if any. An A2S-only
    # session (no site account has claimed this steam_id) is a no-op; PlayerRecord update
    # above proceeds unchanged either way.
    points_cfg = await _get_points_config(db)
    earned = (body.session_seconds // 60) * points_cfg["per_minute"]
    if earned > 0:
        user_res = await db.execute(select(User).where(User.steam_id == body.steam_id))
        earning_user = user_res.scalar_one_or_none()
        if earning_user is not None:
            await _award_points(db, earning_user, earned, "playtime", f"{body.session_seconds}s session on server {body.server_num}")

    await db.commit()
    return {"success": True}


@router.post("/api/plugin/clans/sync")
@limiter.limit("30/minute")
async def plugin_clans_sync(
    request: Request,
    body: PluginClansSyncIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Sent periodically by the plugin with the FULL current in-game clan roster for one
    server. This replaces (not merges) all game_clans rows for body.server_num — the
    plugin always reports its complete current state, so a stale clan that no longer
    exists in-game simply won't be in the next payload and gets dropped here.

    IMPORTANT: GameClanMember.clan_id declares ondelete="CASCADE" at the ORM level, but
    that is NOT actually enforced by the live DB — SQLite only applies ON DELETE CASCADE
    when a connection has run `PRAGMA foreign_keys = ON`, and this app's engine
    (backend/database.py) never sets that pragma, so the cascade is silently a no-op.
    Deleting a GameClan row without also explicitly deleting its GameClanMember rows
    leaves them orphaned-but-still-clan_id-matching, and since SQLite's plain
    `INTEGER PRIMARY KEY` (no AUTOINCREMENT) reuses the lowest free rowid, the next insert
    can get the SAME id, causing every previous cycle's "orphaned" members to silently
    reattach and pile up. So we must delete members explicitly, scoped by clan id, before
    deleting the clans themselves. See models.py for the same note near the FK column."""
    clan_ids_result = await db.execute(
        select(GameClan.id).where(GameClan.server_num == body.server_num)
    )
    clan_ids = [row[0] for row in clan_ids_result.all()]
    if clan_ids:
        await db.execute(
            delete(GameClanMember).where(GameClanMember.clan_id.in_(clan_ids))
        )
    await db.execute(
        delete(GameClan).where(GameClan.server_num == body.server_num)
    )
    for clan_in in body.clans:
        clan = GameClan(
            server_num=body.server_num,
            clan_guid=clan_in.clan_guid,
            name=clan_in.name,
            motto=clan_in.motto or "",
        )
        db.add(clan)
        await db.flush()
        for member_in in clan_in.members:
            db.add(GameClanMember(
                clan_id=clan.id,
                steam_id=member_in.steam_id,
                character_name=member_in.character_name,
                role=member_in.role,
            ))
    await db.commit()
    return {"success": True, "clan_count": len(body.clans)}


@router.get("/api/plugin/announcements")
@limiter.limit("120/minute")
async def plugin_get_announcements(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (piggybacking on its heartbeat interval, ~every 60s) so the
    admin can push scheduled/recurring in-game chat announcements from the site without
    restarting the server/plugin. Replaces the old single-text server_announcement Setting
    with a proper Announcement table (see the "Scheduled Announcements" admin section
    below) supporting one-off and recurring messages. A row is "due" if it's never been
    sent, or (for recurring rows) if interval_minutes have elapsed since last_sent_at —
    once a due row is returned here, last_sent_at is stamped immediately, since the plugin
    is trusted to broadcast on receipt (same resilience level as the rest of this
    integration — no separate delivery-ack round-trip). server_num scopes the poll to a
    single game server (each plugin instance sends its own server_num, matching its config)
    — defaults to 1 for backward compat with an old plugin build that predates per-server
    announcements, but the current plugin always sends it explicitly. Response shape:
    {"announcements": [{"text": str, "target_steam_id": str|null}, ...]} — target_steam_id
    is null for normal (broadcast-to-everyone) rows and a SteamID for one-off test-sends
    created via POST /api/admin/announcements/test-send, which the plugin should deliver
    only to that player instead of broadcasting."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Announcement)
        .where(
            Announcement.enabled == True,  # noqa: E712
            Announcement.server_num == server_num,
            or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
        )
        .order_by(Announcement.created_at.asc())
    )
    rows = result.scalars().all()

    due: list[Announcement] = []
    for a in rows:
        if a.last_sent_at is None:
            due.append(a)
            continue
        if a.interval_minutes is None:
            continue  # one-off announcement already sent — never due again
        last_sent = a.last_sent_at
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        if (now - last_sent).total_seconds() >= a.interval_minutes * 60:
            due.append(a)

    for a in due:
        a.last_sent_at = now
    if due:
        await db.commit()

    return {"announcements": [{"text": a.text, "target_steam_id": a.target_steam_id} for a in due]}


@router.get("/api/plugin/message-templates", response_model=ServerMessageTemplateOut)
@limiter.limit("120/minute")
async def plugin_get_message_templates(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (same cadence as its heartbeat) so the site can drive the
    connect/disconnect in-game chat message text — per-server ServerMessageTemplate rows,
    replacing the old global "connect_message_template"/"disconnect_message_template"
    Settings now that the plugin runs on more than one server — without editing the
    plugin's local BepInEx .cfg and restarting the game server. server_num defaults to 1
    for backward compat with an old plugin build; the current plugin always sends it
    explicitly. Empty string means "not set"; the plugin falls back to its own local
    config default in that case. Response shape: {"connect": str, "disconnect": str}."""
    result = await db.execute(
        select(ServerMessageTemplate).where(ServerMessageTemplate.server_num == server_num)
    )
    row = result.scalar_one_or_none()
    return ServerMessageTemplateOut(
        connect=(row.connect_template or "") if row else "",
        disconnect=(row.disconnect_template or "") if row else "",
    )


# ─── Wipe countdown (plugin) ────────────────────────────────────────────────────
# Backed by the same wipe_date/wipe_type (server 1) and wipe_date2/wipe_type2 (server 2)
# Settings the admin panel's "Вайп" card already writes via a raw <input
# type="datetime-local"> — the stored value is local wall-clock time in the site's
# configured timezone (Setting "timezone"), e.g. "2024-01-15T18:30", NOT UTC.

@router.get("/api/plugin/wipe-info")
@limiter.limit("60/minute")
async def plugin_wipe_info(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Upcoming-wipe countdown for the in-game plugin. Response shape:
    {"wipe_date": "<ISO-8601 UTC, 'Z' suffix>" | null, "wipe_type": str | null}.
    Never raises for a bad/missing value — an unrecognized server_num, an empty/unset
    wipe_date, or an unparseable stored value all just come back as null/null, since this
    is a best-effort in-game countdown display."""
    if server_num == 1:
        date_key, type_key = "wipe_date", "wipe_type"
    elif server_num == 2:
        date_key, type_key = "wipe_date2", "wipe_type2"
    else:
        return {"wipe_date": None, "wipe_type": None}

    result = await db.execute(select(Setting).where(Setting.key.in_([date_key, type_key])))
    settings = {s.key: s.value for s in result.scalars().all()}
    raw_date = (settings.get(date_key) or "").strip()
    if not raw_date:
        return {"wipe_date": None, "wipe_type": None}

    try:
        local_dt = datetime.fromisoformat(raw_date)
    except ValueError:
        return {"wipe_date": None, "wipe_type": None}

    tz = await _site_timezone(db)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    return {"wipe_date": _fmt_dt_z(utc_dt), "wipe_type": settings.get(type_key) or None}


# ─── Player playtime (plugin) ───────────────────────────────────────────────────
# Global (all-servers) total playtime for a linked SteamID, queried directly off
# PlayerRecord.steam_id (set once the plugin reports a real session for that row — see
# PlayerRecord.steam_id's comment in models.py) — no join through User needed since that
# column is already populated by verified session reports. Mirrors the same
# func.sum(PlayerRecord.total_seconds) aggregation used for a player's public profile
# total (GET /api/users/{username}), which likewise sums across every server_num rather
# than scoping to one — this endpoint does the same for consistency.

@router.get("/api/plugin/playtime")
@limiter.limit("60/minute")
async def plugin_playtime(
    request: Request,
    steam_id: str,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """server_num is accepted only for the usual plugin-key resolution in
    _require_plugin_key — the returned total is always global across all servers.
    Response: {"total_seconds": int}, 0 (never null) for an unknown/unregistered
    steam_id."""
    result = await db.execute(
        select(func.sum(PlayerRecord.total_seconds)).where(PlayerRecord.steam_id == steam_id)
    )
    total_seconds = int(result.scalar_one() or 0)
    return {"total_seconds": total_seconds}


# ─── Connect streak (plugin) ────────────────────────────────────────────────────
# Tracks, per player per server, which CALENDAR DAYS (in the site's configured timezone —
# Setting "timezone", same _site_timezone helper as wipe-info/daily-restart above) a
# player connected at least once, so the plugin can show a "you've played N days in a
# row!" message on connect. See PlayerDailyActivity in models.py.

@router.post("/api/plugin/connect-streak")
@limiter.limit("60/minute")
async def plugin_connect_streak(
    request: Request,
    body: PluginConnectStreakIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Records today (site-local calendar date) as active for this steam_id+server_num if
    not already recorded — idempotent, a player connecting more than once in the same day
    doesn't double-insert (PlayerDailyActivity's unique constraint) or inflate the streak —
    then computes the current consecutive-day streak by walking backward one day at a time
    from today until the first day with no activity row. Response: {"streak_days": N},
    N >= 1 since today was just recorded."""
    tz = await _site_timezone(db)
    today = datetime.now(tz).date()
    today_str = today.isoformat()

    existing = await db.execute(
        select(PlayerDailyActivity).where(
            PlayerDailyActivity.server_num == body.server_num,
            PlayerDailyActivity.steam_id == body.steam_id,
            PlayerDailyActivity.activity_date == today_str,
        )
    )
    was_new_today = existing.scalar_one_or_none() is None
    if was_new_today:
        db.add(PlayerDailyActivity(
            server_num=body.server_num,
            steam_id=body.steam_id,
            activity_date=today_str,
        ))
        await db.commit()

    dates_result = await db.execute(
        select(PlayerDailyActivity.activity_date).where(
            PlayerDailyActivity.server_num == body.server_num,
            PlayerDailyActivity.steam_id == body.steam_id,
        )
    )
    active_dates = {row[0] for row in dates_result.all()}

    streak_days = 0
    cursor = today
    while cursor.isoformat() in active_dates:
        streak_days += 1
        cursor -= timedelta(days=1)

    # Streak-bonus earning hook — only on the first connect of the day (was_new_today),
    # never on a same-day idempotent re-poll, and only once the streak reaches the
    # configured minimum. Second, separate commit (streak_days isn't known until after
    # the first commit above already ran).
    points_cfg = await _get_points_config(db)
    if was_new_today and streak_days >= points_cfg["streak_min_days"]:
        user_res = await db.execute(select(User).where(User.steam_id == body.steam_id))
        earning_user = user_res.scalar_one_or_none()
        if earning_user is not None:
            await _award_points(db, earning_user, points_cfg["streak_bonus"], "streak", f"streak day {streak_days}")
            await db.commit()

    return {"streak_days": streak_days}


# ─── Scheduled server restart ──────────────────────────────────────────────────
# An admin sets "restart in N minutes" either from the site admin panel (POST/DELETE
# /api/admin/servers/{server_num}/restart, in backend/routers/server_admin.py) or,
# separately, from an in-game admin chat command that hits the plugin-facing endpoints
# here — both paths share the ScheduledRestart row and the _schedule_restart/
# _cancel_restart helpers (backend/helpers.py) so they can't get out of sync. The plugin
# polls GET .../restart-status (same cadence as its heartbeat) to know when to start
# broadcasting a countdown to players and when to actually execute the restart; it is
# expected to POST cancel-restart itself right after doing so, as cleanup — this endpoint
# makes no distinction between that call and an admin explicitly cancelling a pending
# restart.

def _next_daily_restart_utc(daily_restart_time: str, tz: ZoneInfo) -> datetime:
    """Next occurrence of "HH:MM" (interpreted in the site's configured timezone) as a
    naive UTC datetime — today at that time if it's still in the future relative to "now"
    in that timezone, otherwise tomorrow. Mirrors _schedule_restart's naive-UTC storage
    convention for ScheduledRestart.restart_at."""
    hour, minute = (int(p) for p in daily_restart_time.split(":"))
    now_local = datetime.now(tz)
    candidate_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate_local <= now_local:
        candidate_local += timedelta(days=1)
    return candidate_local.astimezone(timezone.utc).replace(tzinfo=None)


@router.get("/api/plugin/restart-status")
@limiter.limit("120/minute")
async def plugin_restart_status(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (same cadence as its heartbeat, ~every 60s). Response shape:
    {"restart_at": "<ISO-8601 UTC, 'Z' suffix>" | null} — null means no restart pending.

    Also self-arms a recurring daily_restart_time (see the "Recurring daily restart"
    section below): if restart_at is currently null but daily_restart_time is set for
    this server_num, computes the next occurrence and persists it into restart_at right
    here — as if an admin had just scheduled a one-off restart for that moment — so this
    poll (and every other reader of the row, including the admin panel) sees it
    immediately. No separate cron/background scheduler needed: once the plugin executes
    the restart and calls POST /api/plugin/cancel-restart (which clears restart_at but
    deliberately leaves daily_restart_time untouched), the very next poll here re-arms
    the following day's occurrence automatically."""
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    if row is not None and row.restart_at is None and row.daily_restart_time:
        tz = await _site_timezone(db)
        row.restart_at = _next_daily_restart_utc(row.daily_restart_time, tz)
        await db.commit()
    return {"restart_at": _fmt_dt_z(row.restart_at if row else None)}


@router.post("/api/plugin/schedule-restart")
@limiter.limit("30/minute")
async def plugin_schedule_restart(
    request: Request,
    body: PluginScheduleRestartIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs an in-game admin chat command (e.g. "restart in 10 minutes"). Overwrites
    any previously scheduled restart for body.server_num rather than stacking."""
    restart_at = await _schedule_restart(db, body.server_num, body.minutes)
    return {"restart_at": _fmt_dt_z(restart_at)}


@router.post("/api/plugin/cancel-restart")
@limiter.limit("30/minute")
async def plugin_cancel_restart(
    request: Request,
    body: PluginCancelRestartIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Idempotent — 200 with no error whether or not a restart was actually pending."""
    await _cancel_restart(db, body.server_num)
    return {"success": True}
