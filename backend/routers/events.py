import html
import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from jose import jwt as jose_jwt

from ..database import get_db
from ..models import User, Event, EventParticipant, RevokedToken, Setting
from ..auth import get_admin_user, get_current_user, SECRET_KEY, ALGORITHM, COOKIE_NAME
from ..helpers import _fmt_dt, _audit, activity_broadcast

router = APIRouter()

# Repo mount path for frontend/events.html inside the production container — mirrors
# main.py's _INDEX_HTML_PATH (used by /api/news-embed). Kept local to this router
# rather than imported from main.py to avoid a circular import (main.py imports this
# router module at startup).
_EVENTS_HTML_PATH = "/opt/vrising-site/frontend/events.html"


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

    # Batch both per-event lookups that used to run once per row (an N+1 for the
    # count, and another N+1 for the viewer's own membership check) into two queries
    # total for the whole page, then assemble in Python below.
    event_ids = [ev.id for ev in rows]
    participant_counts: dict[int, int] = {}
    joined_ids: set[int] = set()
    if event_ids:
        count_rows = (await db.execute(
            select(EventParticipant.event_id, func.count())
            .where(EventParticipant.event_id.in_(event_ids))
            .group_by(EventParticipant.event_id)
        )).all()
        participant_counts = {eid: cnt for eid, cnt in count_rows}
        if current_user_id:
            joined_rows = (await db.execute(
                select(EventParticipant.event_id).where(
                    EventParticipant.event_id.in_(event_ids),
                    EventParticipant.user_id == current_user_id,
                )
            )).scalars().all()
            joined_ids = set(joined_rows)

    items = []
    for ev in rows:
        items.append({
            "id": ev.id, "title": ev.title, "description": ev.description,
            "event_type": ev.event_type, "start_date": _fmt_dt(ev.start_date),
            "end_date": _fmt_dt(ev.end_date), "max_participants": ev.max_participants,
            "status": ev.status, "cover_url": ev.cover_url,
            "created_by": ev.created_by, "created_at": _fmt_dt(ev.created_at),
            "participant_count": participant_counts.get(ev.id, 0),
            "is_joined": ev.id in joined_ids,
        })
    return {"items": items, "total": total}


@router.get("/api/events/mine/next")
async def my_next_event(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Nearest upcoming/active event the current user is registered for — powers the
    homepage "Продолжить" widget (frontend/index.js). Ordered by start_date ascending
    with no lower bound on start_date itself: an "active" event may have already
    started (start_date in the past) and should still outrank a later "upcoming" one,
    so the status filter alone (not a start_date >= now filter) decides eligibility.
    Registered above GET /api/events/{event_id} for the same reason
    /api/clans/leaderboard is registered above /api/clans/{clan_id} — see that route's
    comment; a literal "mine" segment here is a different path shape (2 segments after
    /api/events) so it can't actually collide, but keeping the ordering convention
    consistent avoids relitigating it later."""
    row = (await db.execute(
        select(Event)
        .join(EventParticipant, EventParticipant.event_id == Event.id)
        .where(
            EventParticipant.user_id == current_user.id,
            Event.status.in_(("upcoming", "active")),
        )
        .order_by(Event.start_date.asc())
        .limit(1)
    )).scalar_one_or_none()
    if row is None:
        return {"event": None}
    return {"event": {
        "id": row.id, "title": row.title, "event_type": row.event_type,
        "start_date": _fmt_dt(row.start_date), "end_date": _fmt_dt(row.end_date),
        "status": row.status,
    }}


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
    activity_broadcast({
        "type": "event", "title": ev.title,
        "subtitle": f"Начало: {_fmt_dt(ev.start_date)}" if ev.start_date else "",
        "url": "/events.html", "icon": "📅", "timestamp": _fmt_dt(ev.created_at),
    })
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
    # Single batched user lookup instead of one db.get(User, ...) per row.
    user_ids = [ep.user_id for ep in rows]
    users_by_id: dict[int, User] = {}
    if user_ids:
        user_rows = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        users_by_id = {u.id: u for u in user_rows}
    result = []
    for ep in rows:
        u = users_by_id.get(ep.user_id)
        result.append({
            "user_id": ep.user_id,
            "username": u.username if u else str(ep.user_id),
            "avatar_url": u.avatar_url if u else None,
            "registered_at": _fmt_dt(ep.registered_at),
        })
    return result


# ─── Link-unfurl embed ───────────────────────────────────────────────────────
# Mirrors GET /api/news-embed in backend/main.py: link-unfurlers (Discord/Telegram/VK/
# Twitter, most search bots) don't run JS, so they never see events.html's client-side
# setEventsJsonLd()/meta swap and would otherwise always show the generic events-list
# title/description/image for every shared event link, no matter which event it was.
# Re-uses frontend/events.html itself (read from the repo mount) so layout/styling never
# drifts out of sync — only the meta tag values are swapped before serving. Kept in this
# router rather than main.py (unlike news-embed) since this slice's scope is limited to
# events.py/events.html; wiring nginx's crawler-UA map (see nginx/nginx*.conf's
# $is_crawler_ua, which currently only proxies "/?news=" requests to /api/news-embed) to
# also route "/events.html?event=" crawler requests here is a follow-up outside that
# scope — the endpoint itself is functional and tested standalone in the meantime.
_EVENTS_EMBED_META_PATTERNS = [
    (re.compile(r'(<title id="page-title">).*?(</title>)'), "title"),
    (re.compile(r'(<meta id="meta-description"[^>]*content=")[^"]*(")'), "desc"),
    (re.compile(r'(<link rel="canonical" href=")[^"]*(")'), "url"),
    (re.compile(r'(<meta property="og:url" content=")[^"]*(")'), "url"),
    (re.compile(r'(<meta id="meta-og-title"[^>]*content=")[^"]*(")'), "title"),
    (re.compile(r'(<meta id="meta-og-description"[^>]*content=")[^"]*(")'), "desc"),
    (re.compile(r'(<meta property="og:image" content=")[^"]*(")'), "image"),
]


@router.get("/api/events-embed")
async def events_embed(id: int, db: AsyncSession = Depends(get_db)):
    """Server-rendered <head> meta for one event, for crawlers that don't run JS. Falls
    back to the page's default meta for an unknown/missing event id, same as news-embed."""
    try:
        with open(_EVENTS_HTML_PATH, "r", encoding="utf-8") as f:
            page = f.read()
    except OSError as e:
        raise HTTPException(status_code=404, detail="events.html not found") from e

    ev = (await db.execute(select(Event).where(Event.id == id))).scalar_one_or_none()
    if ev is None:
        return Response(content=page, media_type="text/html; charset=utf-8")

    base_url = "https://v.just-skill.ru"
    try:
        su_res = await db.execute(select(Setting).where(Setting.key == "https_domain"))
        su = su_res.scalar_one_or_none()
        if su and su.value.strip():
            base_url = f"https://{su.value.strip()}"
    except Exception:
        pass

    image = ev.cover_url or f"{base_url}/uploads/og-default.png"
    if image.startswith("/"):
        image = base_url + image
    plain_desc = re.sub(r"<[^>]+>", "", ev.description or ev.title).strip()[:160]
    link = f"{base_url}/events.html?event={ev.id}"

    values = {
        "title": html.escape(f"{ev.title} — V Rising"),
        "desc": html.escape(plain_desc),
        "url": html.escape(link),
        "image": html.escape(image),
    }
    for pattern, key in _EVENTS_EMBED_META_PATTERNS:
        page = pattern.sub(lambda m, v=values[key]: m.group(1) + v + m.group(2), page, count=1)

    return Response(content=page, media_type="text/html; charset=utf-8")


# ─── Calendar export (.ics) ─────────────────────────────────────────────────────
# Lets a player subscribe to server events in their own calendar app instead of having
# to remember to check events.html — a plain static file, no auth, so it works as a
# "webcal://" subscription URL a calendar app polls periodically, not just a one-time
# download. RFC 5545 requires CRLF line endings and escaping ",;\" and literal
# newlines in text fields.

def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _ics_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_vevent(ev: Event) -> str:
    end = ev.end_date or ev.start_date
    lines = [
        "BEGIN:VEVENT",
        f"UID:event-{ev.id}@just-skill.ru",
        f"DTSTAMP:{_ics_dt(datetime.now(timezone.utc))}",
        f"DTSTART:{_ics_dt(ev.start_date)}",
        f"DTEND:{_ics_dt(end)}",
        f"SUMMARY:{_ics_escape(ev.title)}",
    ]
    if ev.description:
        lines.append(f"DESCRIPTION:{_ics_escape(ev.description)}")
    if ev.status == "cancelled":
        lines.append("STATUS:CANCELLED")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def _ics_calendar(vevents: list[str]) -> str:
    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Just-Skill.Ru//Events//RU",
        "CALSCALE:GREGORIAN",
        *vevents,
        "END:VCALENDAR",
        "",
    ])


@router.get("/api/events/{event_id}/ics")
async def get_event_ics(event_id: int, db: AsyncSession = Depends(get_db)):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    body = _ics_calendar([_ics_vevent(ev)])
    return Response(
        content=body, media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="event-{ev.id}.ics"'},
    )


@router.get("/api/events.ics")
async def get_events_calendar_feed(db: AsyncSession = Depends(get_db)):
    """Full feed (not paginated — a calendar app expects the whole subscription in one
    response) of every non-cancelled event, past or future, so a client that already
    subscribed doesn't lose history it cached. Excludes cancelled events entirely
    rather than including them as CANCELLED VEVENTs — simpler, and this repo has no
    "was this ever real" audit need for a calendar feed the way the moderation log
    does."""
    rows = (await db.execute(
        select(Event).where(Event.status != "cancelled").order_by(Event.start_date.asc())
    )).scalars().all()
    body = _ics_calendar([_ics_vevent(ev) for ev in rows])
    return Response(
        content=body, media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="events.ics"'},
    )
