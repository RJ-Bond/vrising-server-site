import csv
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, case

from ..database import get_db
from ..models import User, News, Comment, Setting, AuditLog, PageView, ErrorLog, PointsTransaction, ShopRedemption
from ..auth import get_admin_user, get_moderator_user, is_at_least
from ..helpers import UPLOAD_DIR, send_newsletter_digest, _audit
from ..schemas import CommentBulkDeleteIn, CommentBulkResult, CommentBulkDeleteOut

router = APIRouter()


# ─── Discord Webhook ─────────────────────────────────────────────────────────
# _discord_webhook_news (the new-post announce helper) moved to
# backend/routers/news.py — its only caller, POST /api/admin/news, lives there now.

@router.post("/api/admin/test-webhook")
async def test_discord_webhook(request: Request, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    try:
        body_data = await request.json()
        url = (body_data.get("url") or "").strip()
    except Exception:
        url = ""
    if not url:
        res = await db.execute(select(Setting).where(Setting.key == "discord_webhook_url"))
        setting = res.scalar_one_or_none()
        url = (setting.value or "").strip() if setting else ""
    if not url or "discord" not in url or "/api/webhooks/" not in url:
        raise HTTPException(status_code=400, detail="Discord Webhook URL не настроен — введите URL в поле выше")
    try:
        embed = {
            "title": "✅ Тест вебхука — V Rising",
            "description": "Вебхук настроен корректно. Уведомления о новостях будут появляться здесь.",
            "color": 0x00B050,
            "footer": {"text": "V Rising Admin Panel"},
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json={"embeds": [embed]}, timeout=10.0)
        if r.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail=f"Discord вернул {r.status_code}: {r.text[:300]}")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса: {type(e).__name__}: {e}") from e


# ─── Newsletter digest ────────────────────────────────────────────────────────
# Manual "send it now" action for the weekly opt-in news digest (see
# send_newsletter_digest() in helpers.py and _newsletter_digest_task in main.py,
# which normally fires this same function every Monday). admin tier, not
# superadmin — this is a content/comms action, not infra/role-management, same
# tier as test_discord_webhook above.

@router.post("/api/admin/newsletter/send-now")
async def send_newsletter_digest_now(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    summary = await send_newsletter_digest(db)
    return summary


# ─── Dashboard stats ─────────────────────────────────────────────────────────

@router.get("/api/admin/stats")
async def admin_stats(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    user_count    = (await db.execute(select(func.count(User.id)))).scalar_one()
    news_count    = (await db.execute(select(func.count(News.id)))).scalar_one()
    comment_count = (await db.execute(select(func.count(Comment.id)))).scalar_one()
    file_count    = sum(1 for f in UPLOAD_DIR.iterdir() if f.is_file())
    recent_comments = (await db.execute(
        select(Comment, News.title.label("ntitle"), News.slug.label("nslug"),
               User.username.label("uname"))
        .join(News, Comment.news_id == News.id)
        .outerjoin(User, Comment.author_id == User.id)
        .order_by(Comment.created_at.desc()).limit(5)
    )).all()
    return {
        "user_count": user_count, "news_count": news_count,
        "comment_count": comment_count, "file_count": file_count,
        "recent_comments": [
            {"id": r.Comment.id, "content": r.Comment.content[:120],
             "news_title": r.ntitle, "news_slug": r.nslug,
             "author": r.uname or "Аноним",
             "created_at": r.Comment.created_at.isoformat()}
            for r in recent_comments
        ],
    }


# ─── Comments moderation ─────────────────────────────────────────────────────

@router.get("/api/admin/comments")
async def list_all_comments(
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    q: str = Query(""),
    news_slug: Optional[str] = Query(None),
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(Comment.content.ilike(like), User.username.ilike(like), News.title.ilike(like)))
    if news_slug:
        filters.append(News.slug == news_slug)
    count_q = select(func.count(Comment.id)).join(News, Comment.news_id == News.id).outerjoin(User, Comment.author_id == User.id).where(*filters)
    total = (await db.execute(count_q)).scalar_one()
    rows = (await db.execute(
        select(Comment, News.id.label("news_id"), News.title.label("ntitle"), News.slug.label("nslug"),
               User.id.label("uid"), User.username.label("uname"), User.avatar_url.label("uavatar"))
        .join(News, Comment.news_id == News.id)
        .outerjoin(User, Comment.author_id == User.id)
        .where(*filters)
        .order_by(Comment.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [
            {
                "id": r.Comment.id,
                "content": r.Comment.content[:200],
                "created_at": r.Comment.created_at.isoformat(),
                "user_id": r.uid,
                "username": r.uname or "Аноним",
                "avatar_url": r.uavatar,
                "news_id": r.news_id,
                "news_slug": r.nslug,
                "news_title": r.ntitle,
            }
            for r in rows
        ],
    }


@router.delete("/api/admin/comments/{comment_id}", status_code=204)
async def admin_delete_comment(
    comment_id: int,
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    await db.delete(comment)
    await db.commit()


@router.post("/api/admin/comments/bulk-delete", response_model=CommentBulkDeleteOut)
async def admin_bulk_delete_comments(
    body: CommentBulkDeleteIn,
    current_user: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk variant of DELETE /api/admin/comments/{id} — same moderator tier. Per-id
    success/failure rather than one all-or-nothing transaction: some ids in the batch
    might already be deleted (by another moderator, or the author themselves) or never
    have existed, and that shouldn't 500 the rest of the batch — same defensive pattern
    as POST /api/admin/points/grant-bulk in routers/points_shop.py."""
    results: list[CommentBulkResult] = []
    deleted_ids: list[int] = []
    for cid in body.ids:
        result = await db.execute(select(Comment).where(Comment.id == cid))
        comment = result.scalar_one_or_none()
        if comment is None:
            results.append(CommentBulkResult(id=cid, success=False, error="Комментарий не найден"))
            continue
        await db.delete(comment)
        deleted_ids.append(cid)
        results.append(CommentBulkResult(id=cid, success=True))
    if deleted_ids:
        await _audit(
            db, current_user.id, "comment.bulk_delete", target_type="comment",
            detail=f"{len(deleted_ids)} ids: {deleted_ids[:50]}",
        )
    await db.commit()
    succeeded = sum(1 for r in results if r.success)
    return CommentBulkDeleteOut(results=results, succeeded=succeeded, failed=len(results) - succeeded)


# ─── Audit log ───────────────────────────────────────────────────────────────

# Action names written only by endpoints gated at get_superadmin_user (role changes,
# backup handling, RCON, and — after clear_moderation_log — the moderation-log purge).
# Kept as an explicit allowlist (not e.g. "any action containing 'backup'") so a future
# admin-tier action can never accidentally leak into the superadmin-only filtered view
# just by sharing a substring. Cross-checked against `grep get_superadmin_user
# backend/routers/*.py` — every superadmin-gated endpoint that actually calls
# _audit()/log_audit() today is listed here.
#
# NOTE — known gap, not fixed in this pass: /api/admin/ssl/install (SSL install),
# /api/admin/update (git-pull site update) and /api/admin/update/check are also
# get_superadmin_user-gated but don't call _audit()/log_audit() at all yet, so those
# actions never show up here or in the unfiltered log either. Backup download/create/
# delete and RCON below were judged worth wiring up now (single mutating/sensitive
# action each); the two deploy endpoints stream progress over SSE and would need more
# surgery to log without double-writing on retries — left for a follow-up.
SUPERADMIN_AUDIT_ACTIONS = {
    "user.role",
    "bulk_role_change",
    "rcon_command",
    "clear_moderation_log",
    "backup.download",
    "backup.create",
    "backup.delete",
}


@router.get("/api/admin/audit-log/actions")
async def get_audit_log_actions(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action))).scalars().all()
    return rows


@router.get("/api/admin/audit-log")
async def get_audit_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    q: str = Query(""),
    action: str = Query(""),
    tier: str = Query(""),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """tier="superadmin" narrows the feed to SUPERADMIN_AUDIT_ACTIONS — a moderator/admin
    can call this endpoint at all (get_admin_user, unchanged), but only a superadmin may
    actually request that filter; anyone else asking for it gets a 403 rather than a
    silently-empty/ignored param, so the frontend's superadmin-only filter toggle (see
    admin.html) can't be worked around by hand-editing the query string."""
    if tier.strip() == "superadmin" and not is_at_least(current_user, "superadmin"):
        raise HTTPException(status_code=403, detail="Requires superadmin access")
    filters = []
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(AuditLog.admin_username.ilike(like), AuditLog.detail.ilike(like)))
    if action.strip():
        filters.append(AuditLog.action == action.strip())
    if tier.strip() == "superadmin":
        filters.append(AuditLog.action.in_(SUPERADMIN_AUDIT_ACTIONS))
    total = (await db.execute(select(func.count(AuditLog.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(AuditLog).where(*filters).order_by(AuditLog.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [
            {
                "id": r.id,
                "admin": r.admin_username,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


# ─── Analytics (page views) ───────────────────────────────────────────────────

@router.get("/api/admin/analytics")
async def get_analytics(
    days: int = Query(7, ge=1, le=90),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(
            func.date(PageView.created_at).label("day"),
            func.count(PageView.id).label("views"),
            func.count(func.distinct(PageView.ip_hash)).label("unique"),
        )
        .where(PageView.created_at >= cutoff)
        .group_by(func.date(PageView.created_at))
        .order_by(func.date(PageView.created_at).asc())
    )).all()

    top_pages = (await db.execute(
        select(PageView.path, func.count(PageView.id).label("cnt"))
        .where(PageView.created_at >= cutoff)
        .group_by(PageView.path)
        .order_by(func.count(PageView.id).desc())
        .limit(10)
    )).all()

    total_views = (await db.execute(
        select(func.count(PageView.id)).where(PageView.created_at >= cutoff)
    )).scalar_one()

    # Extended analytics: totals, users_by_day, top_news
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    thirty_days_ago_naive = thirty_days_ago.replace(tzinfo=None)
    seven_days_ago_naive = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)

    total_users = (await db.execute(select(func.count(User.id)))).scalar_one()
    total_news_count = (await db.execute(
        select(func.count(News.id)).where(News.published == True)
    )).scalar_one()
    total_comments_count = (await db.execute(select(func.count(Comment.id)))).scalar_one()
    active_users_7d = (await db.execute(
        select(func.count(User.id)).where(
            User.last_active_at.isnot(None),
            User.last_active_at >= seven_days_ago_naive,
        )
    )).scalar_one()

    users_by_day_rows = (await db.execute(
        select(
            func.strftime("%Y-%m-%d", User.created_at).label("date"),
            func.count(User.id).label("count"),
        )
        .where(User.created_at >= thirty_days_ago_naive)
        .group_by(func.strftime("%Y-%m-%d", User.created_at))
        .order_by(func.strftime("%Y-%m-%d", User.created_at).asc())
    )).all()

    top_news_rows = (await db.execute(
        select(
            News.slug, News.title, News.views,
            func.count(Comment.id).label("comment_count"),
        )
        .outerjoin(Comment, News.id == Comment.news_id)
        .where(News.published == True)
        .group_by(News.id)
        .order_by(News.views.desc())
        .limit(10)
    )).all()

    return {
        "days": days,
        "total_views": total_views,
        "by_day": [{"day": r.day, "views": r.views, "unique": r.unique} for r in rows],
        "top_pages": [{"path": r.path, "views": r.cnt} for r in top_pages],
        "totals": {
            "users": total_users,
            "news": total_news_count,
            "comments": total_comments_count,
            "active_users_7d": active_users_7d,
        },
        "users_by_day": [{"date": r.date, "count": r.count} for r in users_by_day_rows],
        "top_news": [
            {"slug": r.slug, "title": r.title, "views": r.views, "comment_count": r.comment_count}
            for r in top_news_rows
        ],
    }


# ─── Points economy dashboard ──────────────────────────────────────────────────

@router.get("/api/admin/economy-stats")
async def get_economy_stats(
    days: int = Query(30, ge=7, le=90),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Points issued vs spent per day (same unit — one axis, a legitimate grouped
    bar, not the dual-axis anti-pattern) plus the shop's most-redeemed items, for
    the admin "Экономика" dashboard. PointsTransaction is the append-only ledger
    (see its model docstring) — delta > 0 is an earn/grant, delta < 0 is a spend/
    refund-reversal, so day/direction is a straight aggregate over it, no denormalized
    counter needed."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    issued_expr = func.sum(case((PointsTransaction.delta > 0, PointsTransaction.delta), else_=0))
    spent_expr = func.sum(case((PointsTransaction.delta < 0, -PointsTransaction.delta), else_=0))
    by_day_rows = (await db.execute(
        select(
            func.date(PointsTransaction.created_at).label("day"),
            issued_expr.label("issued"),
            spent_expr.label("spent"),
        )
        .where(PointsTransaction.created_at >= cutoff)
        .group_by(func.date(PointsTransaction.created_at))
        .order_by(func.date(PointsTransaction.created_at).asc())
    )).all()

    top_items_rows = (await db.execute(
        select(
            ShopRedemption.item_name_snapshot,
            func.count(ShopRedemption.id).label("redemptions"),
            func.sum(ShopRedemption.cost_snapshot).label("points_spent"),
        )
        .where(ShopRedemption.status != "cancelled", ShopRedemption.created_at >= cutoff)
        .group_by(ShopRedemption.item_name_snapshot)
        .order_by(func.count(ShopRedemption.id).desc())
        .limit(10)
    )).all()

    balance_total = (await db.execute(select(func.sum(User.points_balance)))).scalar_one() or 0

    return {
        "days": days,
        "balance_total": balance_total,
        "by_day": [{"day": r.day, "issued": r.issued or 0, "spent": r.spent or 0} for r in by_day_rows],
        "top_items": [
            {"name": r.item_name_snapshot, "redemptions": r.redemptions, "points_spent": r.points_spent or 0}
            for r in top_items_rows
        ],
    }


# ─── CSV export ───────────────────────────────────────────────────────────────

@router.get("/api/admin/export/users")
async def export_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "email", "role", "is_active", "created_at", "last_active_at"])
    for u in users:
        w.writerow([u.id, u.username, u.email, u.role, u.is_active, u.created_at, u.last_active_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


@router.get("/api/admin/export/audit-log")
async def export_audit_log(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = (await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc())
    )).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "admin_username", "action", "target_type", "target_id", "detail", "created_at"])
    for r in rows:
        w.writerow([r.id, r.admin_username, r.action, r.target_type, r.target_id, r.detail, r.created_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )


@router.get("/api/admin/export/bans")
async def export_bans(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_moderator_user),
):
    # Banned users are those with is_active=False
    rows = (await db.execute(
        select(User).where(User.is_active == False).order_by(User.created_at.desc())
    )).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "email", "role", "created_at"])
    for u in rows:
        w.writerow([u.id, u.username, u.email, u.role, u.created_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bans.csv"},
    )


@router.get("/api/admin/export/points-transactions")
async def export_player_points_transactions(
    user_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Per-player CSV export of the full points ledger — a filtered variant of GET
    /api/admin/points/transactions (points_shop.py, which already supports the same
    user_id filter for the JSON/paginated view), for the "Экономика" dashboard's
    per-player history export card. Admin tier (matching that endpoint's tier — the
    global export_redemptions above is moderator-tier, but points/grant-bulk and the
    rest of the manual-grant tooling this pairs with are admin-only, so this stays
    consistent with that rather than with the redemptions export)."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "User not found")
    rows = (await db.execute(
        select(PointsTransaction).where(PointsTransaction.user_id == user_id)
        .order_by(PointsTransaction.created_at.desc())
    )).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "delta", "balance_after", "reason", "detail"])
    for t in rows:
        w.writerow([t.created_at, t.delta, t.balance_after, t.reason, t.detail])
    buf.seek(0)
    # Filename is admin-authored data (the site's own username, not attacker input in
    # any meaningful sense — same trust level as the other filenames in this file) but
    # sanitized anyway since Content-Disposition treats these as a structured header,
    # not free text.
    safe_username = re.sub(r"[^A-Za-z0-9_-]", "_", user.username)[:64] or str(user.id)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=points_{safe_username}.csv"},
    )


@router.get("/api/admin/export/redemptions")
async def export_redemptions(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_moderator_user),
):
    rows = (await db.execute(
        select(ShopRedemption, User.username)
        .join(User, User.id == ShopRedemption.user_id)
        .order_by(ShopRedemption.created_at.desc())
    )).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user", "item", "cost", "status", "created_at", "resolved_at", "resolved_by", "admin_note"])
    for r, username in rows:
        w.writerow([username, r.item_name_snapshot, r.cost_snapshot, r.status, r.created_at, r.resolved_at, r.resolved_by, r.admin_note])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=redemptions.csv"},
    )


# ─── Error log ────────────────────────────────────────────────────────────────

@router.get("/api/admin/errors")
async def get_error_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    q: str = Query(""),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(ErrorLog.path.ilike(like), ErrorLog.error.ilike(like)))
    total = (await db.execute(select(func.count(ErrorLog.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(ErrorLog).where(*filters).order_by(ErrorLog.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {
        "total": total,
        "items": [
            {"id": r.id, "path": r.path, "method": r.method,
             "status_code": r.status_code, "error": r.error,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }
