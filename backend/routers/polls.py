from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import User, News, Poll, PollOption, PollVote
from ..auth import get_admin_user, get_current_user
from ..rate_limit import limiter
from ..schemas import PollCreate

router = APIRouter()


# ─── Polls ────────────────────────────────────────────────────────────────────

@router.post("/api/news/{slug}/poll", status_code=201)
async def create_poll(
    slug: str,
    body: PollCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    existing = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Poll already exists for this news")
    poll = Poll(
        news_id=news.id,
        question=body.question,
        multiple=body.multiple,
        ends_at=body.ends_at,
    )
    db.add(poll)
    await db.flush()
    for opt in body.options:
        db.add(PollOption(poll_id=poll.id, text=opt.text))
    await db.commit()
    return {"ok": True, "poll_id": poll.id}


@router.get("/api/news/{slug}/poll")
async def get_poll(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    poll = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if not poll:
        return None
    # count votes per option
    vote_rows = (await db.execute(
        select(PollVote.option_id, func.count(PollVote.id).label("cnt"))
        .where(PollVote.poll_id == poll.id)
        .group_by(PollVote.option_id)
    )).all()
    vote_map = {r.option_id: r.cnt for r in vote_rows}
    total_votes = sum(vote_map.values())

    # get user voted options
    user_voted: list[int] = []
    try:
        current_user = await get_current_user(request=request, db=db)
        if current_user:
            uv = (await db.execute(
                select(PollVote.option_id).where(PollVote.poll_id == poll.id, PollVote.user_id == current_user.id)
            )).scalars().all()
            user_voted = list(uv)
    except Exception:
        pass

    return {
        "id": poll.id,
        "news_id": poll.news_id,
        "question": poll.question,
        "multiple": poll.multiple,
        "ends_at": poll.ends_at.isoformat() if poll.ends_at else None,
        "created_at": poll.created_at.isoformat(),
        "total_votes": total_votes,
        "user_voted": user_voted,
        "options": [
            {"id": o.id, "text": o.text, "votes": vote_map.get(o.id, 0)}
            for o in poll.options
        ],
    }


@router.post("/api/news/{slug}/poll/vote")
@limiter.limit("10/minute")
async def vote_poll(
    request: Request,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    option_ids = body.get("option_ids", [])
    if not option_ids:
        raise HTTPException(400, "option_ids required")

    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    poll = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if not poll:
        raise HTTPException(404, "Poll not found")
    _ends_at = poll.ends_at.replace(tzinfo=timezone.utc) if poll.ends_at and poll.ends_at.tzinfo is None else poll.ends_at
    if _ends_at and datetime.now(timezone.utc) > _ends_at:
        raise HTTPException(400, "Poll has ended")

    existing = (await db.execute(
        select(PollVote).where(PollVote.poll_id == poll.id, PollVote.user_id == current_user.id)
    )).scalars().all()
    if existing:
        raise HTTPException(400, "Already voted")

    if not poll.multiple:
        option_ids = option_ids[:1]

    for oid in option_ids:
        opt = (await db.execute(select(PollOption).where(PollOption.id == oid, PollOption.poll_id == poll.id))).scalar_one_or_none()
        if not opt:
            raise HTTPException(400, f"Invalid option id {oid}")
        db.add(PollVote(poll_id=poll.id, option_id=oid, user_id=current_user.id))
    await db.commit()
    return {"ok": True}


@router.delete("/api/news/{slug}/poll", status_code=204)
async def delete_poll(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    poll = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if poll:
        await db.delete(poll)
        await db.commit()
