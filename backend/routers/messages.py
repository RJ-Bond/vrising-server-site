import asyncio
import json
from typing import Optional

from pydantic import BaseModel, field_validator
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import User, Message, Notification
from ..auth import get_current_user, get_admin_user
from ..helpers import _audit, _dm_sse_clients, dm_broadcast, send_push
from ..rate_limit import limiter
from ..schemas import strip_html_tags

router = APIRouter()


# ─── Direct Messages ─────────────────────────────────────────────────────────

class MessageSendBody(BaseModel):
    recipient_username: str
    content: str

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        v = strip_html_tags(v).strip()
        if not v:
            raise ValueError("Сообщение не может быть пустым")
        if len(v) > 2000:
            raise ValueError("Максимум 2000 символов")
        return v


@router.post("/api/messages", status_code=201)
@limiter.limit("30/minute")
async def send_message(
    request: Request,
    body: MessageSendBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.recipient_username == current_user.username:
        raise HTTPException(status_code=400, detail="Нельзя писать самому себе")
    res = await db.execute(select(User).where(User.username == body.recipient_username, User.is_active == True))
    recipient = res.scalar_one_or_none()
    if recipient is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    msg = Message(sender_id=current_user.id, recipient_id=recipient.id, content=body.content.strip())
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    # DMs previously had no notification at all — the recipient only found out by
    # opening the inbox themselves. Same Notification mechanism as comment replies/
    # mentions, "message" type (see the notif-bell rendering in index.js/common.js).
    db.add(Notification(
        user_id=recipient.id,
        type="message",
        data=json.dumps({
            "from_username": current_user.username,
            "preview": msg.content[:100],
        }, ensure_ascii=False),
    ))
    await db.commit()
    # Real-time push to the recipient's open /api/messages/stream connection(s), if
    # any — replaces index.js's old 5s poll of the open conversation. Payload mirrors
    # the Notification.data above; the client re-fetches on receipt rather than
    # trusting this raw payload as the source of truth (see dm_broadcast()'s docstring).
    dm_broadcast(recipient.id, {
        "from_username": current_user.username,
        "preview": msg.content[:100],
    })
    asyncio.create_task(send_push(
        recipient.id,
        "Новое сообщение",
        f"{current_user.username}: {msg.content[:100]}",
        f"/?dm={current_user.username}",
    ))
    return {
        "id": msg.id,
        "sender": current_user.username,
        "recipient": recipient.username,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
    }


class BroadcastBody(BaseModel):
    content: str
    role: Optional[str] = None  # None = every active user; else "user"|"moderator"|"admin"|"superadmin"

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        v = strip_html_tags(v).strip()
        if not v:
            raise ValueError("Сообщение не может быть пустым")
        if len(v) > 2000:
            raise ValueError("Максимум 2000 символов")
        return v


@router.post("/api/admin/broadcast", status_code=201)
async def broadcast_message(
    body: BroadcastBody,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Sends body.content as a DM (same Message/Notification path as a normal 1:1
    message — appears in the recipient's inbox, triggers the same notif-bell entry)
    from the admin to every active user, optionally filtered to one role. There was
    previously no way to reach more than one player at a time short of a per-user DM
    loop by hand. A plain per-recipient insert loop, not a background job — fine at
    this site's real user count; would need rethinking well before that stopped being
    true."""
    q = select(User).where(User.is_active == True, User.id != current_user.id)
    if body.role:
        q = q.where(User.role == body.role)
    recipients = (await db.execute(q)).scalars().all()
    if not recipients:
        raise HTTPException(400, "Нет получателей")

    content = body.content.strip()
    for recipient in recipients:
        db.add(Message(sender_id=current_user.id, recipient_id=recipient.id, content=content))
        db.add(Notification(
            user_id=recipient.id, type="message",
            data=json.dumps({"from_username": current_user.username, "preview": content[:100]}, ensure_ascii=False),
        ))
    await _audit(db, current_user.id, "broadcast.send", target_type="broadcast", target_id=None,
                 detail=f"{len(recipients)} получателей ({body.role or 'все'}): {content[:100]}")
    await db.commit()
    for recipient in recipients:
        dm_broadcast(recipient.id, {
            "from_username": current_user.username,
            "preview": content[:100],
        })
        asyncio.create_task(send_push(
            recipient.id,
            "Сообщение от администрации",
            f"{current_user.username}: {content[:100]}",
            f"/?dm={current_user.username}",
        ))
    return {"sent": len(recipients)}


@router.get("/api/messages/unread-count")
async def messages_unread_count(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(func.count()).where(Message.recipient_id == current_user.id, Message.read == False)
    )
    return {"count": res.scalar_one() or 0}


@router.get("/api/messages/inbox")
async def messages_inbox(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    partner_ids_q = (
        select(
            func.coalesce(
                func.nullif(Message.sender_id, current_user.id),
                Message.recipient_id
            ).label("partner_id"),
            func.max(Message.id).label("last_msg_id"),
        )
        .where((Message.sender_id == current_user.id) | (Message.recipient_id == current_user.id))
        .group_by("partner_id")
        .order_by(func.max(Message.id).desc())
    )
    rows = (await db.execute(partner_ids_q)).all()
    if not rows:
        return []

    # Previously one SELECT per partner for the User row, one per last Message, and one
    # COUNT per unread tally — N+1 three times over for an inbox with N conversations.
    # Batched into 3 total queries (partners, last messages, unread counts-by-partner)
    # regardless of how many conversations there are.
    partner_ids = [row.partner_id for row in rows]
    last_msg_ids = [row.last_msg_id for row in rows]

    partners_res = await db.execute(select(User).where(User.id.in_(partner_ids)))
    partners_by_id = {u.id: u for u in partners_res.scalars().all()}

    msgs_res = await db.execute(select(Message).where(Message.id.in_(last_msg_ids)))
    last_msg_by_id = {m.id: m for m in msgs_res.scalars().all()}

    unread_res = await db.execute(
        select(Message.sender_id, func.count())
        .where(
            Message.sender_id.in_(partner_ids),
            Message.recipient_id == current_user.id,
            Message.read == False,
        )
        .group_by(Message.sender_id)
    )
    unread_by_partner = dict(unread_res.all())

    conversations = []
    for row in rows:
        partner = partners_by_id.get(row.partner_id)
        if partner is None:
            continue
        last_msg = last_msg_by_id.get(row.last_msg_id)
        conversations.append({
            "partner": {"id": partner.id, "username": partner.username, "avatar_url": partner.avatar_url},
            "last_message": {
                "id": last_msg.id,
                "content": last_msg.content,
                "sender_id": last_msg.sender_id,
                "created_at": last_msg.created_at.isoformat(),
            } if last_msg else None,
            "unread": unread_by_partner.get(row.partner_id, 0),
        })
    return conversations


@router.get("/api/messages/with/{username}")
async def messages_conversation(
    username: str,
    before_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(User).where(User.username == username, User.is_active == True))
    partner = res.scalar_one_or_none()
    if partner is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    base = (
        select(Message)
        .where(
            ((Message.sender_id == current_user.id) & (Message.recipient_id == partner.id))
            | ((Message.sender_id == partner.id) & (Message.recipient_id == current_user.id))
        )
    )
    if before_id:
        base = base.where(Message.id < before_id)
    msgs_res = await db.execute(base.order_by(Message.id.desc()).limit(51))
    batch = msgs_res.scalars().all()
    has_more = len(batch) > 50
    messages = list(reversed(batch[:50]))

    for m in messages:
        if m.recipient_id == current_user.id and not m.read:
            m.read = True
    await db.commit()

    return {
        "partner": {"id": partner.id, "username": partner.username, "avatar_url": partner.avatar_url},
        "has_more": has_more,
        "messages": [
            {
                "id": m.id,
                "sender": m.sender.username,
                "content": m.content,
                "read": m.read,
                "created_at": m.created_at.isoformat(),
                "is_mine": m.sender_id == current_user.id,
            }
            for m in messages
        ],
    }


@router.delete("/api/messages/{msg_id}", status_code=204)
async def delete_message(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Message).where(Message.id == msg_id, Message.sender_id == current_user.id))
    msg = res.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    await db.delete(msg)
    await db.commit()


@router.get("/api/messages/stream")
async def messages_stream(current_user: User = Depends(get_current_user)):
    """Push a small notice to the current user whenever they receive a new DM
    (helpers.dm_broadcast(), called from send_message()/broadcast_message() above),
    instead of index.js re-polling GET /api/messages/with/<partner> on a 5s timer
    while a DM panel is open. Same shape as GET /api/activity-feed/stream (bounded
    per-client queue, 25s keepalive comment, X-Accel-Buffering: no) with one
    difference: this stream is per-user, not a public flat broadcast, so it's
    behind get_current_user (same auth dependency every other /api/messages/*
    route uses) and registers its queue under _dm_sse_clients[current_user.id]
    rather than in a single flat set. EventSource can't send an Authorization
    header, but it does send cookies on same-origin requests by default, and this
    site's auth is cookie-based (see auth.py's COOKIE_NAME) — so get_current_user
    resolves the same way here as it does for a normal fetch() call."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _dm_sse_clients.setdefault(current_user.id, set()).add(queue)

    async def generate():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            queues = _dm_sse_clients.get(current_user.id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    _dm_sse_clients.pop(current_user.id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
