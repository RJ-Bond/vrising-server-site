import csv
import io
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from ..database import get_db
from ..models import User, Ban, Warning, BanAppeal, ModerationLogEntry, WarnEscalationState, Notification
from ..auth import get_admin_user, get_superadmin_user
from ..rate_limit import limiter
from ..helpers import _require_plugin_key, _fmt_dt_z, _force_unban, _audit, _get_server_names, _get_linked_usernames
from ..schemas import (
    PluginWarnIn,
    PluginBanIn,
    PluginUnbanIn,
    PluginLogActionIn,
    BanAppealCreate,
    AppealResolveIn,
)

router = APIRouter()


@router.post("/api/plugin/warn")
@limiter.limit("30/minute")
async def plugin_warn(
    request: Request,
    body: PluginWarnIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .warn admin chat command. Logs a moderation warning against
    body.steam_id; warning_count is the player's total across all servers/time,
    including the one just inserted.

    should_escalate decides whether the plugin should auto-ban this player, computed here
    (not by the plugin comparing warning_count >= threshold itself) so it only fires once
    per threshold's worth of NEW warnings rather than on every single .warn once a player's
    lifetime count sits above the threshold — see WarnEscalationState's docstring for the
    bug this replaced. threshold <= 0 means auto-escalation is disabled (plugin's
    WarnEscalationThreshold = 0), in which case should_escalate is always false and no
    state row is touched."""
    db.add(Warning(
        server_num=body.server_num,
        steam_id=body.steam_id,
        character_name=body.character_name,
        reason=body.reason,
        admin_name=body.admin_name,
        created_at=datetime.utcnow(),
    ))
    await db.commit()
    count_result = await db.execute(select(func.count()).where(Warning.steam_id == body.steam_id))
    warning_count = count_result.scalar_one()

    should_escalate = False
    if body.threshold > 0:
        state = await db.get(WarnEscalationState, body.steam_id)
        last_count = state.last_escalation_count if state else 0
        if warning_count - last_count >= body.threshold:
            should_escalate = True
            if state:
                state.last_escalation_count = warning_count
            else:
                db.add(WarnEscalationState(steam_id=body.steam_id, last_escalation_count=warning_count))
            await db.commit()

    return {"success": True, "warning_count": warning_count, "should_escalate": should_escalate}


@router.get("/api/plugin/warnings")
@limiter.limit("60/minute")
async def plugin_warnings(
    request: Request,
    steam_id: str,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .warnings admin chat command — lists ALL warnings for
    steam_id across every server, most recent first. server_num is accepted only for
    the usual API-key resolution in _require_plugin_key; results are never filtered
    by it, since a player's warning history should follow them across servers."""
    result = await db.execute(
        select(Warning).where(Warning.steam_id == steam_id).order_by(Warning.created_at.desc())
    )
    warnings = result.scalars().all()
    return {
        "warnings": [
            {
                "reason": w.reason,
                "admin_name": w.admin_name,
                "created_at": _fmt_dt_z(w.created_at),
                "server_num": w.server_num,
            }
            for w in warnings
        ]
    }


# ─── Player bans (plugin) ──────────────────────────────────────────────────────
# Backs the in-game .ban/.unban admin chat commands. The game engine itself performs the
# real ban/unban (native ban events) — these routes are only site-side record-keeping plus
# auto-expiry scheduling for temp bans. See models.Ban's docstring for the full lifecycle.

@router.post("/api/plugin/ban")
@limiter.limit("30/minute")
async def plugin_ban(
    request: Request,
    body: PluginBanIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Logs a new ban (permanent if body.unban_at is null, temp otherwise) issued via the
    in-game .ban admin chat command. unban_at is normalized to naive UTC before storing
    (this repo's usual DateTime convention) regardless of what offset/format it arrived in."""
    unban_at = body.unban_at
    if unban_at is not None and unban_at.tzinfo is not None:
        unban_at = unban_at.astimezone(timezone.utc).replace(tzinfo=None)
    db.add(Ban(
        server_num=body.server_num,
        steam_id=body.steam_id,
        character_name=body.character_name,
        admin_name=body.admin_name,
        reason=body.reason,
        banned_at=datetime.utcnow(),
        unban_at=unban_at,
    ))
    await db.commit()
    return {"success": True}


@router.post("/api/plugin/unban")
@limiter.limit("30/minute")
async def plugin_unban(
    request: Request,
    body: PluginUnbanIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Called by the plugin right after it manually executes a .unban in-game. Resolves
    whatever active ban(s) exist for this steam_id across ALL servers (not just
    body.server_num) — matches the cross-server enforcement in GET /api/plugin/ban-status:
    a ban issued on one server now blocks connecting to every tracked server, so lifting it
    must clear it everywhere too, including when an admin runs .unban on a different server
    than the one that originally issued the ban. Idempotent — 200 even if nothing was
    active."""
    result = await db.execute(
        select(Ban).where(
            Ban.steam_id == body.steam_id,
            Ban.unbanned_at.is_(None),
        )
    )
    active = result.scalars().all()
    if active:
        now = datetime.utcnow()
        for b in active:
            b.unbanned_at = now
        await db.commit()
    return {"success": True}


@router.get("/api/plugin/due-unbans")
@limiter.limit("60/minute")
async def plugin_due_unbans(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (same cadence as its heartbeat) so it knows which players to
    actually unban in-game. Returns active bans (unbanned_at IS NULL) for server_num whose
    unban_at has passed — covers BOTH a temp ban's timer naturally expiring AND an admin
    force-unbanning from the site's bans admin page (which just sets unban_at to "now").
    Same "returning due items also consumes them" pattern as GET /api/plugin/announcements
    above: each due row is stamped unbanned_at immediately, since the plugin is trusted to
    actually execute the unban on receipt. Never returns permanent bans (unban_at NULL)."""
    now = datetime.utcnow()
    result = await db.execute(
        select(Ban).where(
            Ban.server_num == server_num,
            Ban.unbanned_at.is_(None),
            Ban.unban_at.isnot(None),
            Ban.unban_at <= now,
        )
    )
    due = result.scalars().all()
    for b in due:
        b.unbanned_at = now
    if due:
        await db.commit()
    return {"unbans": [{"steam_id": b.steam_id, "character_name": b.character_name} for b in due]}


@router.get("/api/plugin/ban-status")
@limiter.limit("60/minute")
async def plugin_ban_status(
    request: Request,
    steam_id: str,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Checked by the plugin on every player connect as a workaround for the game
    engine's own native ban enforcement not reliably rejecting an already-banned player.
    Cross-server: a ban issued on ANY of the site's tracked servers now blocks connecting
    to ALL of them — server_num is accepted for backward compatibility but no longer used
    to filter; looks up any currently-active Ban (unbanned_at IS NULL) for this steam_id
    regardless of which server originally issued it. If more than one is somehow active at
    once, a permanent one wins over a temporary one, then the most recently issued.
    unban_at NULL in the response means a permanent ban."""
    result = await db.execute(
        select(Ban)
        .where(Ban.steam_id == steam_id, Ban.unbanned_at.is_(None))
        .order_by(Ban.unban_at.is_(None).desc(), Ban.banned_at.desc())
    )
    ban = result.scalars().first()
    if ban is None:
        return {"banned": False}
    return {
        "banned": True,
        "admin_name": ban.admin_name,
        "reason": ban.reason,
        "unban_at": _fmt_dt_z(ban.unban_at),
    }


@router.get("/api/plugin/ban-lookup")
@limiter.limit("30/minute")
async def plugin_ban_lookup(
    request: Request,
    character_name: str,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs ".unban <ник>" — a banned player is offline by definition, so the plugin can't
    resolve a nickname to a steam_id through the game's own online-player lookup the way
    every other moderation command does; this is the site-side fallback (the site is the
    only place that still has character_name recorded against the ban). Case-insensitive
    exact match against active bans (unbanned_at IS NULL) only — a lifted ban's old
    character_name is irrelevant here. character_name isn't unique (a nickname could
    coincidentally collide, or be reused after a rename) so ties go to the most recently
    issued active ban, same tiebreak convention as plugin_ban_status."""
    result = await db.execute(
        select(Ban)
        .where(func.lower(Ban.character_name) == character_name.lower(), Ban.unbanned_at.is_(None))
        .order_by(Ban.banned_at.desc())
    )
    ban = result.scalars().first()
    if ban is None:
        return {"found": False, "steam_id": None}
    return {"found": True, "steam_id": ban.steam_id}


_VALID_LOG_ACTIONS = {"kick", "mute", "unmute", "restart_scheduled", "restart_executed", "report"}


@router.post("/api/plugin/log-action")
@limiter.limit("60/minute")
async def plugin_log_action(
    request: Request,
    body: PluginLogActionIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Records the moderation action types NOT already covered by their own dedicated
    endpoints — ban/unban -> POST /api/plugin/ban /unban, warn -> POST /api/plugin/warn —
    for the unified feed at GET /api/admin/moderation-log. Only the values in
    _VALID_LOG_ACTIONS are accepted (400 "invalid_action" otherwise) to avoid
    double-counting ban/unban/warn once merged into that feed. "report" (added for the
    plugin's ".report" command) reuses admin_name for the REPORTING player rather than an
    admin — it's the only action here not actually performed by an admin."""
    if body.action not in _VALID_LOG_ACTIONS:
        raise HTTPException(status_code=400, detail="invalid_action")
    db.add(ModerationLogEntry(
        server_num=body.server_num,
        action=body.action,
        admin_name=body.admin_name,
        target_name=body.target_name,
        target_steam_id=body.target_steam_id,
        details=body.details,
        created_at=datetime.utcnow(),
    ))
    await db.commit()
    return {"success": True}


# ─── Player bans (admin) ───────────────────────────────────────────────────────
# Admin-panel counterpart to the POST /api/plugin/ban / unban / GET .../due-unbans trio
# above — see models.Ban's docstring for the full active/unban_at/unbanned_at lifecycle.

@router.get("/api/admin/bans")
async def list_bans(
    status: str = Query(default="active"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Bans across every server, most recent first. status controls which ones:
    "active" (default — preserves the original behavior of this endpoint, since existing
    callers assume active-only) = unbanned_at IS NULL; "resolved" = unbanned_at IS NOT
    NULL (already lifted, read-only history for the admin UI); "all" = no filter. Any
    other value falls back to "active". unban_at null means permanent; a timestamp is the
    scheduled expiry, for the admin UI to compute/display a "time remaining" countdown.
    unbanned_at (null unless the ban has actually been lifted) is always included so the
    "resolved" view can show when it was lifted. server_name is included as a convenience
    (same server_num -> real-name lookup used by GET /api/clans) so the admin page doesn't
    need a second round-trip just to label the server column. linked_username (null unless
    the banned steam_id matches a registered, active site account) lets bans.html link the
    character name to that account's public profile."""
    server_names = await _get_server_names(db)
    query = select(Ban).order_by(Ban.banned_at.desc())
    if status == "resolved":
        query = query.where(Ban.unbanned_at.is_not(None))
    elif status == "all":
        pass
    else:
        query = query.where(Ban.unbanned_at.is_(None))
    result = await db.execute(query)
    bans = result.scalars().all()
    linked_usernames = await _get_linked_usernames(db, (b.steam_id for b in bans))
    return {
        "bans": [
            {
                "id": b.id,
                "server_num": b.server_num,
                "server_name": server_names.get(b.server_num) or f"Сервер {b.server_num}",
                "steam_id": b.steam_id,
                "character_name": b.character_name,
                "admin_name": b.admin_name,
                "reason": b.reason,
                "banned_at": _fmt_dt_z(b.banned_at),
                "unban_at": _fmt_dt_z(b.unban_at),
                "unbanned_at": _fmt_dt_z(b.unbanned_at),
                "linked_username": linked_usernames.get(b.steam_id),
            }
            for b in bans
        ]
    }


@router.post("/api/admin/bans/{ban_id}/unban")
async def unban_admin(
    ban_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Brings unban_at forward to "now" (regardless of its previous value) rather than
    setting unbanned_at directly — that only happens once the plugin actually confirms the
    real in-game unban via POST /api/plugin/unban or the next GET /api/plugin/due-unbans
    poll consumes this row (within ~60s). 404 if ban_id doesn't exist or is already
    resolved (unbanned_at set)."""
    result = await db.execute(select(Ban).where(Ban.id == ban_id, Ban.unbanned_at.is_(None)))
    ban = result.scalar_one_or_none()
    if ban is None:
        raise HTTPException(status_code=404, detail="Ban not found")
    _force_unban(ban)
    await _audit(db, current_user.id, "ban.unban", target_type="ban", target_id=ban.id, detail=ban.steam_id)
    await db.commit()
    return {"success": True}


# ─── Ban appeals ────────────────────────────────────────────────────────────────
# A banned player is blocked from the GAME SERVER but their SITE account (if any) is
# unaffected, so appealing must work WITHOUT a site login — just the SteamID they can find
# via the Steam client or the ban announcement they saw in-game. See models.BanAppeal's
# docstring for the full lifecycle.

@router.post("/api/appeals")
@limiter.limit("3/hour")
async def submit_ban_appeal(request: Request, body: BanAppealCreate, db: AsyncSession = Depends(get_db)):
    """Public, unauthenticated. Looks up the currently-active Ban for body.steam_id (any
    server_num — most recent if somehow more than one) so random non-banned visitors can't
    spam this; 400 "no_active_ban" if none found. 400 "already_appealed" if a pending
    appeal already exists for that same ban — they wait for a response instead of stacking
    appeals. character_name is taken from the Ban row itself (authoritative) rather than
    the request body, once found."""
    result = await db.execute(
        select(Ban)
        .where(Ban.steam_id == body.steam_id, Ban.unbanned_at.is_(None))
        .order_by(Ban.banned_at.desc())
    )
    ban = result.scalars().first()
    if ban is None:
        raise HTTPException(status_code=400, detail="no_active_ban")

    existing = await db.execute(
        select(BanAppeal).where(BanAppeal.ban_id == ban.id, BanAppeal.status == "pending")
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="already_appealed")

    db.add(BanAppeal(
        ban_id=ban.id,
        steam_id=body.steam_id,
        character_name=ban.character_name or body.character_name,
        message=body.message,
        status="pending",
        created_at=datetime.utcnow(),
    ))
    await db.commit()
    return {"success": True}


@router.get("/api/admin/appeals")
async def list_ban_appeals(
    status: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Most-recent-first; status query param optionally filters to "pending"/"approved"/
    "rejected" (default: all). ban_reason/ban_admin_name are joined through ban_id for
    admin context — null if the underlying Ban row is somehow gone."""
    q = select(BanAppeal).order_by(BanAppeal.created_at.desc())
    if status:
        q = q.where(BanAppeal.status == status)
    result = await db.execute(q)
    appeals = result.scalars().all()

    ban_ids = {a.ban_id for a in appeals if a.ban_id is not None}
    bans_by_id: dict[int, Ban] = {}
    if ban_ids:
        ban_result = await db.execute(select(Ban).where(Ban.id.in_(ban_ids)))
        bans_by_id = {b.id: b for b in ban_result.scalars().all()}

    return {
        "appeals": [
            {
                "id": a.id,
                "steam_id": a.steam_id,
                "character_name": a.character_name,
                "message": a.message,
                "status": a.status,
                "admin_response": a.admin_response,
                "admin_name": a.admin_name,
                "created_at": _fmt_dt_z(a.created_at),
                "resolved_at": _fmt_dt_z(a.resolved_at),
                "ban_reason": bans_by_id[a.ban_id].reason if a.ban_id in bans_by_id else None,
                "ban_admin_name": bans_by_id[a.ban_id].admin_name if a.ban_id in bans_by_id else None,
            }
            for a in appeals
        ]
    }


@router.post("/api/admin/appeals/{appeal_id}/resolve")
async def resolve_ban_appeal(
    appeal_id: int,
    body: AppealResolveIn,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Approving ALSO lifts the underlying ban the exact same way the existing bans.html
    "Разбанить" button does (see _force_unban above) — sets Ban.unban_at to "now", never
    unbanned_at directly. 404 if the appeal doesn't exist or was already resolved."""
    result = await db.execute(
        select(BanAppeal).where(BanAppeal.id == appeal_id, BanAppeal.status == "pending")
    )
    appeal = result.scalar_one_or_none()
    if appeal is None:
        raise HTTPException(status_code=404, detail="Appeal not found")

    appeal.status = "approved" if body.approve else "rejected"
    appeal.admin_response = body.admin_response
    appeal.admin_name = current_user.username
    appeal.resolved_at = datetime.utcnow()

    if body.approve and appeal.ban_id is not None:
        ban_result = await db.execute(select(Ban).where(Ban.id == appeal.ban_id, Ban.unbanned_at.is_(None)))
        ban = ban_result.scalar_one_or_none()
        if ban is not None:
            _force_unban(ban)

    await _audit(db, current_user.id, "appeal.resolve", target_type="ban_appeal", target_id=appeal.id, detail=appeal.status)
    # Appeals are keyed by steam_id, not a site user_id — appellants aren't required to
    # be logged in to submit one, so a notification is only possible if their steam_id
    # happens to match a linked site account. Best-effort, not a hard requirement of
    # resolving the appeal.
    linked_res = await db.execute(select(User).where(User.steam_id == appeal.steam_id))
    linked_user = linked_res.scalar_one_or_none()
    if linked_user is not None:
        db.add(Notification(
            user_id=linked_user.id, type="appeal_resolved",
            data=json.dumps({"approved": body.approve, "admin_response": body.admin_response or ""}, ensure_ascii=False),
        ))
    await db.commit()
    return {"success": True}


# ─── Unified moderation log ─────────────────────────────────────────────────────
# One chronological feed of every moderation action. Ban/Warning already capture
# ban/unban/warn with everything needed, so they're merged in here rather than
# re-logged; ModerationLogEntry (below) only stores the action types those two tables
# don't cover.

@router.get("/api/admin/moderation-log")
async def get_moderation_log(
    limit: int = Query(default=100, le=500),
    server_num: Optional[int] = Query(default=None),
    steam_id: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Merges three sources into one common shape, sorts by timestamp descending in
    Python, then applies limit — result sets here are small and admin-only/infrequent, so
    a UNION SQL query would save little. Ban rows become one "ban" entry each (using
    banned_at) plus, if the ban has actually been lifted (unbanned_at IS NOT NULL), a
    second "unban" entry (using unbanned_at) — there's no site-side tracking of WHO
    unbanned distinctly from the ban's original admin, so admin_name is reused rather than
    left null. Warning rows become "warn" entries (reason -> details). ModerationLogEntry
    rows (kick/mute/unmute/restart_scheduled/restart_executed, written by
    POST /api/plugin/log-action) pass through as-is. steam_id, when given, narrows the feed
    to just that player (matched against each source's own steam-id column — target_steam_id
    in the merged output) — layered on top of the server_num filter, e.g. from bans.html's
    "История" link into this page for one banned player."""
    ban_q = select(Ban)
    warn_q = select(Warning)
    log_q = select(ModerationLogEntry)
    if server_num is not None:
        ban_q = ban_q.where(Ban.server_num == server_num)
        warn_q = warn_q.where(Warning.server_num == server_num)
        log_q = log_q.where(ModerationLogEntry.server_num == server_num)
    if steam_id is not None:
        ban_q = ban_q.where(Ban.steam_id == steam_id)
        warn_q = warn_q.where(Warning.steam_id == steam_id)
        log_q = log_q.where(ModerationLogEntry.target_steam_id == steam_id)

    bans = (await db.execute(ban_q)).scalars().all()
    warnings = (await db.execute(warn_q)).scalars().all()
    log_entries = (await db.execute(log_q)).scalars().all()

    entries = []
    for b in bans:
        entries.append({
            "action": "ban",
            "server_num": b.server_num,
            "admin_name": b.admin_name,
            "target_name": b.character_name,
            "target_steam_id": b.steam_id,
            "details": b.reason,
            "created_at": b.banned_at,
        })
        if b.unbanned_at is not None:
            entries.append({
                "action": "unban",
                "server_num": b.server_num,
                "admin_name": b.admin_name,
                "target_name": b.character_name,
                "target_steam_id": b.steam_id,
                "details": None,
                "created_at": b.unbanned_at,
            })
    for w in warnings:
        entries.append({
            "action": "warn",
            "server_num": w.server_num,
            "admin_name": w.admin_name,
            "target_name": w.character_name,
            "target_steam_id": w.steam_id,
            "details": w.reason,
            "created_at": w.created_at,
        })
    for e in log_entries:
        entries.append({
            "action": e.action,
            "server_num": e.server_num,
            "admin_name": e.admin_name,
            "target_name": e.target_name,
            "target_steam_id": e.target_steam_id,
            "details": e.details,
            "created_at": e.created_at,
        })

    entries.sort(key=lambda e: e["created_at"], reverse=True)
    entries = entries[:limit]
    return {
        "log": [{**e, "created_at": _fmt_dt_z(e["created_at"])} for e in entries]
    }


@router.get("/api/admin/export/moderation-log")
async def export_moderation_log(
    server_num: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """CSV counterpart to GET /api/admin/moderation-log above — same merge of
    Ban/Warning/ModerationLogEntry (so warnings ARE covered here, just not as their own
    separate export), but unlimited rather than capped at `limit` since an export is
    meant to be the complete record, not a page of it."""
    entries = (await get_moderation_log(limit=1_000_000, server_num=server_num, db=db, _=None))["log"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["action", "server_num", "admin_name", "target_name", "target_steam_id", "details", "created_at"])
    for e in entries:
        w.writerow([e["action"], e["server_num"], e["admin_name"], e["target_name"], e["target_steam_id"], e["details"], e["created_at"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=moderation_log.csv"},
    )


@router.get("/api/admin/export/appeals")
async def export_appeals(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = (await db.execute(select(BanAppeal).order_by(BanAppeal.created_at.desc()))).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "steam_id", "character_name", "status", "message", "admin_name", "admin_response", "created_at", "resolved_at"])
    for a in rows:
        w.writerow([a.id, a.steam_id, a.character_name, a.status, a.message, a.admin_name, a.admin_response, a.created_at, a.resolved_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=appeals.csv"},
    )


@router.delete("/api/admin/moderation-log")
async def clear_moderation_log(
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """Superadmin-only: clears only the "safe" part of the unified feed — every
    ModerationLogEntry row (kick/mute/unmute/restart_*/report, pure log data with no other
    purpose) plus already-resolved Ban rows (unbanned_at IS NOT NULL, pure history at that
    point). Deliberately does NOT touch active bans (unbanned_at IS NULL — the Ban row IS
    the enforcement record; deleting it would unban the player) or Warning rows (deleting
    those would reset players' warning counts, including the .warn auto-escalation
    threshold tracking). BanAppeal rows are untouched too — appeals keep their own steam_id
    independent of ban_id specifically so they survive a purged Ban row (see BanAppeal's
    docstring)."""
    log_result = await db.execute(delete(ModerationLogEntry))
    ban_result = await db.execute(delete(Ban).where(Ban.unbanned_at.is_not(None)))
    log_count = log_result.rowcount or 0
    ban_count = ban_result.rowcount or 0
    await _audit(db, current_user.id, "clear_moderation_log", detail=f"{log_count} log entries, {ban_count} resolved bans")
    await db.commit()
    return {"success": True, "deleted_log_entries": log_count, "deleted_resolved_bans": ban_count}


# ─── Public bans list ────────────────────────────────────────────────────────

@router.get("/api/bans")
async def list_public_bans(db: AsyncSession = Depends(get_db)):
    """Public, unauthenticated list of currently-active in-game bans for bans.html's
    anonymous visitors. character_name and reason are shown deliberately: in-game
    server bans are ordinary server-transparency content (like a public
    rules-violations board), not sensitive personal data — there's no real name,
    email, or other PII involved, just a game character name and why it was banned.
    Same "active" semantics as GET /api/admin/bans's default (Ban.unbanned_at IS
    NULL) and the same row shape that endpoint returns, minus steam_id/unbanned_at
    (no reason to publish a player's SteamID, and "resolved" history has no public
    view). No unban capability here — that stays admin-only via
    POST /api/admin/bans/{id}/unban, which bans.html calls directly once it
    separately confirms admin via /api/auth/me and shows an extra action column.
    This briefly (74b07ba) returned just {"active_bans": N} instead, on the theory
    that names/reasons were too sensitive to publish — that instinct turned out not
    to match what's actually wanted for this page, so it's back to a full list.
    linked_username (null unless the banned steam_id matches a registered, active site
    account) lets bans.html link the character name to that account's public profile —
    steam_id itself stays unpublished here, only the (already-public-elsewhere) username
    is exposed."""
    server_names = await _get_server_names(db)
    result = await db.execute(
        select(Ban).where(Ban.unbanned_at.is_(None)).order_by(Ban.banned_at.desc())
    )
    bans = result.scalars().all()
    linked_usernames = await _get_linked_usernames(db, (b.steam_id for b in bans))
    return {
        "bans": [
            {
                "id": b.id,
                "server_num": b.server_num,
                "server_name": server_names.get(b.server_num) or f"Сервер {b.server_num}",
                "character_name": b.character_name,
                "admin_name": b.admin_name,
                "reason": b.reason,
                "banned_at": _fmt_dt_z(b.banned_at),
                "unban_at": _fmt_dt_z(b.unban_at),
                "linked_username": linked_usernames.get(b.steam_id),
            }
            for b in bans
        ]
    }
