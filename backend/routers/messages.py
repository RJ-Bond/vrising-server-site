import json
from typing import Optional

from pydantic import BaseModel, field_validator
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import User, Message, Notification
from ..auth import get_current_user
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
    return {
        "id": msg.id,
        "sender": current_user.username,
        "recipient": recipient.username,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
    }


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
