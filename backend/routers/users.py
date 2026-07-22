import re
from datetime import datetime, timezone

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text

from ..database import get_db
from ..models import User, PlayerRecord, GameClan, GameClanMember, Comment, Reaction, News
from ..auth import get_moderator_user, get_admin_user, get_superadmin_user, role_level
from ..helpers import log_audit, _audit, _fmt_dt
from ..schemas import UserOut, LinkedAccountOut

router = APIRouter()


# ─── Users (admin) ───────────────────────────────────────────────────────────

@router.get("/api/admin/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_moderator_user),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.put("/api/admin/users/{user_id}/role")
async def change_role(
    user_id: int,
    role: str = Query(..., regex="^(user|moderator|admin|superadmin)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_superadmin_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change own role")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.role = role
    await log_audit(db, current_user, "user.role", f"{user.username} → {role}")
    await db.commit()
    return {"ok": True}


@router.put("/api/admin/users/{user_id}/toggle-active")
async def toggle_active(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_moderator_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if role_level(user.role) >= role_level(current_user.role):
        raise HTTPException(status_code=403, detail="Cannot act on a user with equal or higher access level")
    user.is_active = not user.is_active
    _ban_action = "user.unban" if user.is_active else "user.ban"
    await _audit(db, current_user.id, _ban_action, target_type="user", target_id=user.id, detail=user.username)
    await db.commit()
    return {"ok": True, "is_active": user.is_active}


@router.delete("/api/admin/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if role_level(user.role) >= role_level(current_user.role):
        raise HTTPException(status_code=403, detail="Cannot act on a user with equal or higher access level")
    await db.delete(user)
    await log_audit(db, current_user, "user.delete", user.username)
    await db.commit()


@router.post("/api/admin/users/{user_id}/revoke-sessions", status_code=204)
async def revoke_user_sessions(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Force-logout: set revoke_before=now() so all older tokens are rejected on next request."""
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Self-revoke is allowed (unlike delete/toggle-active) — logging yourself out is
    # self-correcting via re-login, so only guard against acting on a DIFFERENT peer/superior.
    if user_id != current_user.id and role_level(target.role) >= role_level(current_user.role):
        raise HTTPException(status_code=403, detail="Cannot act on a user with equal or higher access level")
    now_utc = datetime.now(timezone.utc).isoformat()
    await db.execute(text("UPDATE users SET revoke_before = :ts WHERE id = :uid"), {"ts": now_utc, "uid": user_id})
    await log_audit(db, current_user, "user.revoke_sessions", target.username)
    await db.commit()


# ─── Linked game accounts (admin) ────────────────────────────────────────────
# Site accounts linked to a SteamID via the in-game .register/.login flow (see
# User.steam_id, set by /api/plugin/register and /api/plugin/login above).

@router.get("/api/admin/linked-accounts", response_model=list[LinkedAccountOut])
async def list_linked_accounts(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(
        select(User).where(User.steam_id.isnot(None)).order_by(User.username.asc())
    )
    return [LinkedAccountOut.model_validate(u) for u in result.scalars().all()]


@router.post("/api/admin/users/{user_id}/unlink-steam")
async def unlink_steam_account(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Clears the SteamID link set via the in-game .register/.login flow, e.g. so the
    player can re-link a different game account. Does not touch the site account itself
    (username/password/etc.) — only the link."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.steam_id = None
    await _audit(db, current_user.id, "user.unlink_steam", target_type="user", target_id=user.id, detail=user.username)
    await db.commit()
    return {"ok": True}


# ─── Public profile ──────────────────────────────────────────────────────────

@router.get("/api/users/{username}")
async def get_public_profile(username: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.username == username, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    lookup_name = user.game_nickname or username
    total_result = await db.execute(
        select(func.sum(PlayerRecord.total_seconds)).where(PlayerRecord.player_name == lookup_name)
    )
    total_seconds = total_result.scalar_one() or 0
    last_seen_result = await db.execute(
        select(func.max(PlayerRecord.last_seen)).where(PlayerRecord.player_name == lookup_name)
    )
    last_seen = last_seen_result.scalar_one()
    session_count_result = await db.execute(
        select(func.sum(PlayerRecord.session_count)).where(PlayerRecord.player_name == lookup_name)
    )
    session_count = int(session_count_result.scalar_one() or 0)
    last_dur_result = await db.execute(
        select(PlayerRecord.last_duration).where(
            PlayerRecord.player_name == lookup_name
        ).order_by(PlayerRecord.last_seen.desc()).limit(1)
    )
    last_duration = last_dur_result.scalar_one_or_none() or 0
    # True once at least one PlayerRecord row for this player was claimed by a real
    # /api/plugin/sessions report (steam_id set), vs. total_seconds being purely an
    # older Steam-A2S-polling estimate never confirmed by the plugin.
    verified_result = await db.execute(
        select(PlayerRecord.id).where(
            PlayerRecord.player_name == lookup_name, PlayerRecord.steam_id.isnot(None)
        ).limit(1)
    )
    verified = verified_result.scalar_one_or_none() is not None
    # Clan membership now comes from the game-synced roster (matched via the verified
    # steam_id link), not the old web-managed Clan/User.clan_id system.
    clan = None
    if user.steam_id:
        clan_result = await db.execute(
            select(GameClan).join(GameClanMember, GameClanMember.clan_id == GameClan.id)
            .where(GameClanMember.steam_id == user.steam_id)
        )
        clan_row = clan_result.scalars().first()
        if clan_row:
            clan = {"id": clan_row.id, "name": clan_row.name}
    comment_count_res = await db.execute(
        select(func.count(Comment.id)).where(Comment.author_id == user.id)
    )
    comment_count = comment_count_res.scalar_one() or 0
    return {
        "username": user.username,
        "avatar_url": user.avatar_url,
        "cover_url": user.cover_url,
        "role": user.role,
        "created_at": _fmt_dt(user.created_at),
        "game_nickname": user.game_nickname,
        "total_seconds": total_seconds,
        "last_seen": _fmt_dt(last_seen),
        "session_count": session_count,
        "last_duration": last_duration,
        "verified": verified,
        "clan": clan,
        "admin_title": user.admin_title,
        "last_active_at": _fmt_dt(user.last_active_at),
        "badge_icon_url": user.badge_icon_url,
        "badge_style": user.badge_style or "default",
        "comment_count": comment_count,
    }


# ─── User activity feed ──────────────────────────────────────────────────────

@router.get("/api/users/{username}/activity")
async def get_user_activity(username: str, db: AsyncSession = Depends(get_db)):
    user_res = await db.execute(select(User).where(User.username == username, User.is_active == True))
    user = user_res.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Query last 20 comments by this user
    comments_res = await db.execute(
        select(Comment.id, Comment.content, Comment.created_at, News.slug.label("news_slug"), News.title.label("news_title"))
        .join(News, Comment.news_id == News.id)
        .where(Comment.author_id == user.id)
        .order_by(Comment.created_at.desc())
        .limit(20)
    )
    comment_rows = comments_res.all()

    # Query last 20 reactions by this user
    reactions_res = await db.execute(
        select(Reaction.id, Reaction.emoji, Reaction.created_at, News.slug.label("news_slug"), News.title.label("news_title"))
        .join(News, Reaction.news_id == News.id)
        .where(Reaction.user_id == user.id)
        .order_by(Reaction.created_at.desc())
        .limit(20)
    )
    reaction_rows = reactions_res.all()

    _strip_html = re.compile(r'<[^>]+>')
    items = []
    for r in comment_rows:
        dt = r.created_at
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        preview = _strip_html.sub('', r.content)[:120]
        items.append({"type": "comment", "created_at": _fmt_dt(dt), "news_slug": r.news_slug, "news_title": r.news_title, "preview": preview})
    for r in reaction_rows:
        dt = r.created_at
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        items.append({"type": "reaction", "created_at": _fmt_dt(dt), "news_slug": r.news_slug, "news_title": r.news_title, "emoji": r.emoji})

    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"username": username, "items": items[:20]}


# ─── Bulk user actions ────────────────────────────────────────────────────────

class BulkUserAction(BaseModel):
    user_ids: list[int]
    action: str  # "ban", "unban", "delete"


@router.post("/api/admin/users/bulk")
async def bulk_user_action(
    body: BulkUserAction,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if body.action not in ("ban", "unban", "delete"):
        raise HTTPException(400, "Invalid action")
    if not body.user_ids:
        raise HTTPException(400, "No user IDs provided")
    if len(body.user_ids) > 100:
        raise HTTPException(400, "Too many users (max 100)")

    candidates = (await db.execute(
        select(User).where(User.id.in_(body.user_ids))
    )).scalars().all()
    # Role is a plain string column — filter the hierarchy rule in Python rather than SQL.
    # user_ids is capped at 100 above, so this is cheap.
    rows = [u for u in candidates if role_level(u.role) < role_level(current_user.role)]

    affected = 0
    for u in rows:
        if body.action == "ban":
            u.is_active = False
            affected += 1
        elif body.action == "unban":
            u.is_active = True
            affected += 1
        elif body.action == "delete":
            await db.delete(u)
            affected += 1

    await log_audit(db, current_user, f"bulk_{body.action}", f"ids={body.user_ids} affected={affected}")
    await db.commit()
    return {"affected": affected}
