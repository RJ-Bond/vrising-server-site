import asyncio
import hashlib
import html
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from jose import jwt as jose_jwt

from ..database import get_db
from ..models import User, News, Comment, PageView, Reaction, RevokedToken, CommentReaction, Notification, Setting
from ..auth import get_admin_user, get_current_user, is_at_least, SECRET_KEY, ALGORITHM, COOKIE_NAME
from ..rate_limit import limiter
from ..helpers import _audit, _send_notification_email
from ..schemas import (
    PaginatedNews,
    NewsListOut,
    NewsOut,
    NewsCreate,
    NewsUpdate,
    CommentCreate,
    CommentUpdate,
    CommentOut,
    PaginatedComments,
    ReactBody,
)

router = APIRouter()

logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = re.sub(r"^-+|-+$", "", text)
    return text[:200]


# ─── News (public) ──────────────────────────────────────────────────────────

@router.get("/api/news/tags")
async def list_tags(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(News.tags).where(News.published == True, News.tags != None, News.tags != "")
    )
    all_tags: set[str] = set()
    for (tags_str,) in result.all():
        if tags_str:
            for t in tags_str.split(","):
                t = t.strip()
                if t:
                    all_tags.add(t)
    return sorted(all_tags)


@router.get("/api/news", response_model=PaginatedNews)
async def list_news(
    page: int = Query(1, ge=1),
    per_page: int = Query(5, ge=1, le=50),
    tag: str = Query(None),
    search: str = Query(None, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    base_filter = News.published == True
    if tag:
        base_filter = base_filter & News.tags.contains(tag)
    if search:
        term = f"%{search}%"
        base_filter = base_filter & (News.title.ilike(term) | News.summary.ilike(term))

    total_result = await db.execute(
        select(func.count()).select_from(News).where(base_filter)
    )
    total = total_result.scalar_one()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    result = await db.execute(
        select(News)
        .where(base_filter)
        .order_by(News.pinned.desc(), News.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    items = result.scalars().all()
    news_ids = [n.id for n in items]
    counts: dict[int, int] = {}
    if news_ids:
        cnt_result = await db.execute(
            select(Comment.news_id, func.count(Comment.id))
            .where(Comment.news_id.in_(news_ids))
            .group_by(Comment.news_id)
        )
        counts = {row[0]: row[1] for row in cnt_result.all()}
    out = []
    for n in items:
        d = NewsListOut.model_validate(n)
        d.comment_count = counts.get(n.id, 0)
        out.append(d)
    return PaginatedNews(
        items=out,
        total=total,
        page=page,
        pages=pages,
    )


@router.get("/api/news/{slug}", response_model=NewsOut)
async def get_news(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(News).where(News.slug == slug, News.published == True))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    # Deduplicate views: count only once per IP per 24h using page_views table
    ip = request.client.host if request.client else ""
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else None
    view_key = f"/news/{news.id}"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = await db.execute(
        select(PageView.id).where(
            PageView.path == view_key,
            PageView.ip_hash == ip_hash,
            PageView.created_at >= cutoff,
        ).limit(1)
    )
    news_id = news.id
    if recent.scalar_one_or_none() is None:
        news.views = (news.views or 0) + 1
        db.add(PageView(path=view_key, ip_hash=ip_hash))
    await db.commit()
    result2 = await db.execute(
        select(News).options(selectinload(News.author)).where(News.id == news_id)
    )
    news = result2.scalar_one()
    return NewsOut.model_validate(news)


# ─── Reactions ───────────────────────────────────────────────────────────────

_ALLOWED_EMOJIS = {"fire", "heart", "thumbs_up", "wow"}


@router.get("/api/news/{slug}/reactions")
async def get_reactions(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
):
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(404, "Not found")
    counts_res = await db.execute(
        select(Reaction.emoji, func.count(Reaction.id))
        .where(Reaction.news_id == news_id)
        .group_by(Reaction.emoji)
    )
    counts = {row[0]: row[1] for row in counts_res.all()}
    user_reactions: list[str] = []
    # Try Bearer header first, then cookie
    token: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                uid = int(payload.get("sub", 0))
                if uid:
                    ur_res = await db.execute(
                        select(Reaction.emoji).where(Reaction.news_id == news_id, Reaction.user_id == uid)
                    )
                    user_reactions = [r[0] for r in ur_res.all()]
        except Exception:
            pass
    return {"counts": counts, "user_reactions": user_reactions}


@router.post("/api/news/{slug}/react")
@limiter.limit("30/minute")
async def toggle_reaction(
    request: Request,
    slug: str,
    body: ReactBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    emoji = body.emoji
    if emoji not in _ALLOWED_EMOJIS:
        raise HTTPException(400, "Invalid emoji")
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(404, "Not found")
    existing_res = await db.execute(
        select(Reaction).where(
            Reaction.news_id == news_id,
            Reaction.user_id == current_user.id,
            Reaction.emoji == emoji,
        )
    )
    existing = existing_res.scalar_one_or_none()
    if existing:
        await db.delete(existing)
    else:
        db.add(Reaction(news_id=news_id, user_id=current_user.id, emoji=emoji))
    await db.commit()
    counts_res = await db.execute(
        select(Reaction.emoji, func.count(Reaction.id))
        .where(Reaction.news_id == news_id)
        .group_by(Reaction.emoji)
    )
    counts = {row[0]: row[1] for row in counts_res.all()}
    ur_res = await db.execute(
        select(Reaction.emoji).where(Reaction.news_id == news_id, Reaction.user_id == current_user.id)
    )
    user_reactions = [r[0] for r in ur_res.all()]
    return {"counts": counts, "user_reactions": user_reactions}


# ─── Comments ────────────────────────────────────────────────────────────────

@router.get("/api/news/{slug}/comments", response_model=PaginatedComments)
async def get_comments(
    slug: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(status_code=404, detail="News not found")

    # Fetch every comment for this article in one go and build the reply
    # tree in memory — replies can nest to any depth (reply-to-a-reply),
    # so a single-level query would silently drop grandchild replies.
    all_res = await db.execute(
        select(Comment)
        .where(Comment.news_id == news_id)
        .order_by(Comment.created_at.asc())
        .options(selectinload(Comment.author))
    )
    all_comments = all_res.scalars().all()

    children_by_parent: dict = {}
    top_level = []
    for c in all_comments:
        if c.parent_id is None:
            top_level.append(c)
        else:
            children_by_parent.setdefault(c.parent_id, []).append(c)

    total = len(top_level)
    pages = max(1, math.ceil(total / per_page))
    page_items = top_level[(page - 1) * per_page: (page - 1) * per_page + per_page]

    def serialize_comment(c):
        return {
            "id": c.id,
            "content": c.content,
            "parent_id": c.parent_id,
            "created_at": c.created_at.isoformat(),
            "author": {"id": c.author.id, "username": c.author.username, "avatar_url": c.author.avatar_url, "role": c.author.role, "is_active": c.author.is_active, "created_at": c.author.created_at.isoformat(), "email": ""} if c.author else None,
            "replies": [serialize_comment(r) for r in children_by_parent.get(c.id, [])],
            "reactions": {},
            "user_reaction": None,
        }

    return {"items": [serialize_comment(c) for c in page_items], "total": total, "page": page, "pages": pages}


@router.post("/api/news/{slug}/comments", response_model=CommentOut, status_code=201)
@limiter.limit("20/minute")
async def add_comment(
    request: Request,
    slug: str,
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    news_res = await db.execute(select(News).where(News.slug == slug, News.published == True))
    news = news_res.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    comment = Comment(news_id=news.id, author_id=current_user.id, content=body.content, parent_id=body.parent_id)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    # eager load author
    await db.refresh(comment, ["author"])
    if body.parent_id:
        parent_c = await db.get(Comment, body.parent_id)
        if parent_c and parent_c.author_id and parent_c.author_id != current_user.id:
            db.add(Notification(
                user_id=parent_c.author_id,
                type="reply",
                data=json.dumps({
                    "comment_id": comment.id,
                    "news_slug": slug,
                    "news_title": news.title,
                    "from_username": current_user.username,
                    "preview": body.content[:100]
                }, ensure_ascii=False)
            ))
            await db.commit()
            # Send email notification
            parent_user = await db.get(User, parent_c.author_id)
            if parent_user and parent_user.email:
                # HTML-escape the comment content (freeform user text) before it goes into an
                # HTML email body — otherwise a comment like `<a href="evil">click</a>` renders
                # as a live link in the recipient's inbox, sent from the site's own address.
                safe_username = html.escape(current_user.username)
                safe_title = html.escape(news.title)
                safe_preview = html.escape(body.content[:200])
                asyncio.create_task(_send_notification_email(
                    parent_user.email,
                    f"Новый ответ на ваш комментарий — {news.title}",
                    f"{current_user.username} ответил на ваш комментарий:\n\n{body.content[:200]}\n\nНовость: {news.title}",
                    f"<p><b>{safe_username}</b> ответил на ваш комментарий в новости <b>{safe_title}</b>:</p><blockquote>{safe_preview}</blockquote>",
                ))
    return {
        "id": comment.id,
        "content": comment.content,
        "parent_id": comment.parent_id,
        "created_at": comment.created_at.isoformat(),
        "author": {"id": comment.author.id, "username": comment.author.username, "avatar_url": comment.author.avatar_url, "role": comment.author.role, "is_active": comment.author.is_active, "created_at": comment.author.created_at.isoformat(), "email": ""} if comment.author else None,
        "replies": [],
        "reactions": {},
        "user_reaction": None,
    }


@router.patch("/api/comments/{comment_id}", response_model=CommentOut)
async def update_comment(
    comment_id: int,
    body: CommentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not is_at_least(current_user, "moderator") and comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    comment.content = body.content
    await db.commit()
    await db.refresh(comment)
    return CommentOut.model_validate(comment)


@router.delete("/api/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not is_at_least(current_user, "moderator") and comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    await db.delete(comment)
    await db.commit()


# ─── Comment reactions ───────────────────────────────────────────────────────

ALLOWED_COMMENT_EMOJIS = {"👍", "❤️", "😂", "😮", "😢", "👎"}


@router.post("/api/comments/{comment_id}/react")
async def react_comment(comment_id: int, body: ReactBody, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if body.emoji not in ALLOWED_COMMENT_EMOJIS:
        raise HTTPException(400, "Invalid emoji")
    existing = await db.execute(
        select(CommentReaction).where(
            CommentReaction.comment_id == comment_id,
            CommentReaction.user_id == current_user.id,
            CommentReaction.emoji == body.emoji
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
        return {"toggled": False}
    db.add(CommentReaction(comment_id=comment_id, user_id=current_user.id, emoji=body.emoji))
    await db.commit()
    return {"toggled": True}


@router.get("/api/comments/{comment_id}/reactions")
async def get_comment_reactions(comment_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(CommentReaction.emoji, func.count(CommentReaction.id))
        .where(CommentReaction.comment_id == comment_id)
        .group_by(CommentReaction.emoji)
    )
    counts = {row[0]: row[1] for row in res.all()}
    user_reaction = None
    try:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                uid = int(payload.get("sub", 0))
                if uid:
                    ur = await db.execute(
                        select(CommentReaction.emoji).where(
                            CommentReaction.comment_id == comment_id,
                            CommentReaction.user_id == uid
                        )
                    )
                    user_reaction = ur.scalar_one_or_none()
    except Exception:
        pass
    return {"counts": counts, "user_reaction": user_reaction}


# ─── News admin support: Discord new-post webhook ────────────────────────────
# _discord_webhook_news is used only by POST /api/admin/news (create_news) below to
# announce a newly published article. Physically filed under the "Discord Webhook"
# banner in the pre-split main.py alongside POST /api/admin/test-webhook (which stays
# in main.py — the manual webhook-test endpoint is not part of the News domain and has
# no other caller here), but moved here since its one and only caller is this router.

async def _discord_webhook_news(url: str, news_title: str, news_summary: str, news_slug: str, thumb_url: str = "") -> None:
    if not url or not url.startswith("https://discord.com/api/webhooks/"):
        return
    try:
        embed = {"title": news_title[:256], "description": news_summary[:512], "color": 0xB5002A, "footer": {"text": "V Rising News"}}
        if thumb_url:
            embed["thumbnail"] = {"url": thumb_url}
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"embeds": [embed]}, timeout=5.0)
    except Exception as e:
        logger.warning("Discord webhook error: %s", e)


# ─── News (admin) ────────────────────────────────────────────────────────────

@router.get("/api/admin/news", response_model=PaginatedNews)
async def admin_list_news(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    total_result = await db.execute(select(func.count()).select_from(News))
    total = total_result.scalar_one()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    result = await db.execute(
        select(News).order_by(News.created_at.desc()).offset(offset).limit(per_page)
    )
    items = result.scalars().all()
    return PaginatedNews(
        items=[NewsListOut.model_validate(n) for n in items],
        total=total,
        page=page,
        pages=pages,
    )


@router.get("/api/admin/news/{news_id}", response_model=NewsOut)
async def admin_get_news(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(
        select(News).options(selectinload(News.author)).where(News.id == news_id)
    )
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    return NewsOut.model_validate(news)


@router.post("/api/admin/news", response_model=NewsOut, status_code=201)
async def create_news(
    body: NewsCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    base_slug = slugify(body.title)
    slug = base_slug
    counter = 1
    while True:
        existing = await db.execute(select(News).where(News.slug == slug))
        if existing.scalar_one_or_none() is None:
            break
        slug = f"{base_slug}-{counter}"
        counter += 1
    _published = body.published
    _publish_at = None
    if body.publish_at:
        _pa = body.publish_at
        if _pa.tzinfo is None:
            _pa = _pa.replace(tzinfo=timezone.utc)
        if _pa > datetime.now(timezone.utc):
            _published = False
            _publish_at = _pa
    news = News(
        title=body.title,
        slug=slug,
        summary=body.summary,
        content=body.content,
        thumbnail_url=body.thumbnail_url,
        tags=body.tags or "",
        author_id=current_user.id,
        published=_published,
        publish_at=_publish_at,
        is_template=body.is_template,
    )
    db.add(news)
    await db.flush()  # get news.id for audit
    await _audit(db, current_user.id, "news.create", target_type="news", target_id=news.id, detail=news.title)
    await db.commit()
    await db.refresh(news)
    if news.published:
        wh_res = await db.execute(select(Setting).where(Setting.key == "discord_webhook_url"))
        wh = wh_res.scalar_one_or_none()
        if wh and wh.value:
            asyncio.create_task(_discord_webhook_news(wh.value, news.title, news.summary, news.slug, news.thumbnail_url or ""))
    result = await db.execute(select(News).where(News.id == news.id))
    return NewsOut.model_validate(result.scalar_one())


@router.put("/api/admin/news/{news_id}", response_model=NewsOut)
async def update_news(
    news_id: int,
    body: NewsUpdate,
    db: AsyncSession = Depends(get_db),
    admin_u: User = Depends(get_admin_user),
):
    result = await db.execute(select(News).where(News.id == news_id))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    fields = body.model_fields_set
    if 'title'         in fields: news.title         = body.title
    if 'summary'       in fields: news.summary       = body.summary
    if 'content'       in fields: news.content       = body.content
    if 'thumbnail_url' in fields: news.thumbnail_url = body.thumbnail_url  # None = clear
    if 'tags'          in fields: news.tags          = body.tags
    if 'published'     in fields: news.published     = body.published
    if 'pinned'        in fields: news.pinned        = body.pinned
    if 'publish_at'    in fields:
        _pa = body.publish_at
        if _pa:
            if _pa.tzinfo is None:
                _pa = _pa.replace(tzinfo=timezone.utc)
            if _pa > datetime.now(timezone.utc):
                news.published = False
                news.publish_at = _pa
            else:
                news.publish_at = None
        else:
            news.publish_at = None
    news.updated_at = datetime.now(timezone.utc)
    await _audit(db, admin_u.id, "news.update", target_type="news", target_id=news.id, detail=news.title)
    await db.commit()
    await db.refresh(news)
    result2 = await db.execute(select(News).where(News.id == news.id))
    return NewsOut.model_validate(result2.scalar_one())


@router.delete("/api/admin/news/{news_id}", status_code=204)
async def delete_news(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    admin_u: User = Depends(get_admin_user),
):
    result = await db.execute(select(News).where(News.id == news_id))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    _news_title = news.title
    await db.delete(news)
    await _audit(db, admin_u.id, "news.delete", target_type="news", target_id=news_id, detail=_news_title)
    await db.commit()


# ─── News templates ───────────────────────────────────────────────────────────

@router.get("/api/admin/news/templates")
async def list_templates(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(News).where(News.is_template == True).order_by(News.created_at.desc())
    )).scalars().all()
    return [
        {"id": r.id, "title": r.title, "summary": r.summary,
         "content": r.content, "thumbnail_url": r.thumbnail_url,
         "tags": r.tags, "created_at": r.created_at.isoformat()}
        for r in rows
    ]
