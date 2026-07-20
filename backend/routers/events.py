from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from jose import jwt as jose_jwt

from ..database import get_db
from ..models import User, Event, EventParticipant, RevokedToken
from ..auth import get_admin_user, get_current_user, SECRET_KEY, ALGORITHM, COOKIE_NAME
from ..helpers import _fmt_dt, _audit

router = APIRouter()


# ─── Events & Tournaments ─────────────────────────────────────────────────────

class EventCreate(BaseModel):
    title: str
    description: Optional[str] = None
    event_type: str = "pvp"
    start_date: datetime
    end_date: Optional[datetime] = None
    max_participants: Optional[int] = None
    cover_url: Optional[str] = None


class EventUpdate(EventCreate):
    status: Optional[str] = None


@router.get("/api/events")
async def list_events(
    request: Request,
    status: str = Query("upcoming"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if status:
        filters.append(Event.status == status)
    total = (await db.execute(select(func.count(Event.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(Event).where(*filters)
        .order_by(Event.start_date.asc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()

    # Resolve current user if authenticated
    current_user_id: Optional[int] = None
    try:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                current_user_id = int(payload.get("sub", 0)) or None
    except Exception:
        pass

    items = []
    for ev in rows:
        cnt = (await db.execute(
            select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == ev.id)
        )).scalar_one()
        is_joined = False
        if current_user_id:
            ep = (await db.execute(
                select(EventParticipant).where(
                    EventParticipant.event_id == ev.id,
                    EventParticipant.user_id == current_user_id,
                )
            )).scalar_one_or_none()
            is_joined = ep is not None
        items.append({
            "id": ev.id, "title": ev.title, "description": ev.description,
            "event_type": ev.event_type, "start_date": _fmt_dt(ev.start_date),
            "end_date": _fmt_dt(ev.end_date), "max_participants": ev.max_participants,
            "status": ev.status, "cover_url": ev.cover_url,
            "created_by": ev.created_by, "created_at": _fmt_dt(ev.created_at),
            "participant_count": cnt, "is_joined": is_joined,
        })
    return {"items": items, "total": total}


@router.get("/api/events/{event_id}")
async def get_event(
    event_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    cnt = (await db.execute(
        select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == ev.id)
    )).scalar_one()
    current_user_id: Optional[int] = None
    try:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                current_user_id = int(payload.get("sub", 0)) or None
    except Exception:
        pass
    is_joined = False
    if current_user_id:
        ep = (await db.execute(
            select(EventParticipant).where(
                EventParticipant.event_id == ev.id,
                EventParticipant.user_id == current_user_id,
            )
        )).scalar_one_or_none()
        is_joined = ep is not None
    return {
        "id": ev.id, "title": ev.title, "description": ev.description,
        "event_type": ev.event_type, "start_date": _fmt_dt(ev.start_date),
        "end_date": _fmt_dt(ev.end_date), "max_participants": ev.max_participants,
        "status": ev.status, "cover_url": ev.cover_url,
        "created_by": ev.created_by, "created_at": _fmt_dt(ev.created_at),
        "participant_count": cnt, "is_joined": is_joined,
    }


@router.post("/api/admin/events", status_code=201)
async def admin_create_event(
    body: EventCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = Event(
        title=body.title[:200],
        description=body.description,
        event_type=body.event_type or "pvp",
        start_date=body.start_date,
        end_date=body.end_date,
        max_participants=body.max_participants,
        cover_url=body.cover_url,
        created_by=current_user.id,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    await _audit(db, current_user.id, "event.create", target_type="event", target_id=ev.id, detail=ev.title)
    await db.commit()
    return {"id": ev.id, "title": ev.title, "status": ev.status, "event_type": ev.event_type}


@router.put("/api/admin/events/{event_id}")
async def admin_update_event(
    event_id: int,
    body: EventUpdate,
    admin_u: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    ev.title = body.title[:200]
    ev.description = body.description
    ev.event_type = body.event_type or "pvp"
    ev.start_date = body.start_date
    ev.end_date = body.end_date
    ev.max_participants = body.max_participants
    ev.cover_url = body.cover_url
    if body.status:
        ev.status = body.status
    await _audit(db, admin_u.id, "event.update", target_type="event", target_id=ev.id, detail=ev.title)
    await db.commit()
    await db.refresh(ev)
    cnt = (await db.execute(
        select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == ev.id)
    )).scalar_one()
    return {
        "id": ev.id, "title": ev.title, "description": ev.description,
        "event_type": ev.event_type, "start_date": _fmt_dt(ev.start_date),
        "end_date": _fmt_dt(ev.end_date), "max_participants": ev.max_participants,
        "status": ev.status, "cover_url": ev.cover_url,
        "created_by": ev.created_by, "created_at": _fmt_dt(ev.created_at),
        "participant_count": cnt,
    }


@router.delete("/api/admin/events/{event_id}", status_code=204)
async def admin_delete_event(
    event_id: int,
    admin_u: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    await db.execute(delete(EventParticipant).where(EventParticipant.event_id == event_id))
    await _audit(db, admin_u.id, "event.delete", target_type="event", target_id=event_id, detail=ev.title)
    await db.delete(ev)
    await db.commit()


@router.post("/api/events/{event_id}/join")
async def join_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    if ev.status in ("ended", "cancelled"):
        raise HTTPException(400, "Event is no longer accepting participants")
    existing = (await db.execute(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already joined this event")
    if ev.max_participants is not None:
        cnt = (await db.execute(
            select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == event_id)
        )).scalar_one()
        if cnt >= ev.max_participants:
            raise HTTPException(400, "Event is full")
    db.add(EventParticipant(event_id=event_id, user_id=current_user.id))
    await db.commit()
    return {"ok": True}


@router.delete("/api/events/{event_id}/leave", status_code=204)
async def leave_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ep = (await db.execute(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if ep is None:
        raise HTTPException(404, "Not a participant")
    await db.delete(ep)
    await db.commit()


@router.get("/api/admin/events/{event_id}/participants")
async def admin_event_participants(
    event_id: int,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    rows = (await db.execute(
        select(EventParticipant).where(EventParticipant.event_id == event_id)
        .order_by(EventParticipant.registered_at.asc())
    )).scalars().all()
    result = []
    for ep in rows:
        u = await db.get(User, ep.user_id)
        result.append({
            "user_id": ep.user_id,
            "username": u.username if u else str(ep.user_id),
            "avatar_url": u.avatar_url if u else None,
            "registered_at": _fmt_dt(ep.registered_at),
        })
    return result
