import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from ..database import get_db
from ..models import User, News, Comment, Setting, AuditLog, PageView, ErrorLog
from ..auth import get_admin_user, get_moderator_user
from ..helpers import UPLOAD_DIR

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
        raise HTTPException(status_code=502, detail=f"Ошибка запроса: {type(e).__name__}: {e}")


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


# ─── Audit log ───────────────────────────────────────────────────────────────

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
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(AuditLog.admin_username.ilike(like), AuditLog.detail.ilike(like)))
    if action.strip():
        filters.append(AuditLog.action == action.strip())
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


# ─── Error log ────────────────────────────────────────────────────────────────

@router.get("/api/admin/errors")
async def get_error_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(ErrorLog.id)))).scalar_one()
    rows = (await db.execute(
        select(ErrorLog).order_by(ErrorLog.created_at.desc())
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
