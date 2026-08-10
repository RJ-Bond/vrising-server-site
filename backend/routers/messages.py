import asyncio
import json
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from ..database import get_db
from ..models import User, Message, Notification
from ..auth import get_current_user, get_admin_user
from ..helpers import _audit, _dm_sse_clients, dm_broadcast, send_push, UPLOAD_DIR, optimize_image_bytes
from ..rate_limit import limiter
from ..schemas import strip_html_tags

router = APIRouter()


# ─── Direct Messages ─────────────────────────────────────────────────────────

# DM image attachment upload — a separate multipart step the frontend calls first
# (see POST /api/messages/attachment below) rather than folding into POST /api/messages
# itself, so that endpoint can stay a plain JSON body instead of switching to
# multipart just to support the rare message that has one. 5 MB (not the 10 MB
# _MAX_UPLOAD_BYTES ceiling admin_system.py's generic /api/admin/upload uses) — this
# is a self-service upload any logged-in user can hit at up to 20/minute, not an
# admin-curated asset, so a smaller cap keeps disk usage and per-request latency in
# check. Extension/MIME allowlist mirrors admin_system.py's _ALLOWED_UPLOAD_EXT minus
# .ico (makes no sense as a chat attachment) and with SVG excluded for the same
# script-injection reason documented there.
_DM_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_DM_ATTACHMENT_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_DM_ATTACHMENT_ALLOWED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@router.post("/api/messages/attachment")
@limiter.limit("20/minute")
async def upload_message_attachment(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _DM_ATTACHMENT_ALLOWED_EXT:
        raise HTTPException(400, detail="Допустимые форматы: PNG, JPG, GIF, WebP")
    if file.content_type and file.content_type.split(";")[0].strip() not in _DM_ATTACHMENT_ALLOWED_MIME:
        raise HTTPException(400, detail="Недопустимый MIME-тип файла")
    content = await file.read()
    if len(content) > _DM_ATTACHMENT_MAX_BYTES:
        raise HTTPException(400, detail="Файл слишком большой (максимум 5 МБ)")
    content = optimize_image_bytes(content, suffix)
    dm_dir = UPLOAD_DIR / "dm"
    dm_dir.mkdir(parents=True, exist_ok=True)
    fname = f"dm_{current_user.id}_{uuid.uuid4().hex[:10]}{suffix}"
    (dm_dir / fname).write_bytes(content)
    return {"attachment_url": f"/api/uploads/dm/{fname}"}


class MessageSendBody(BaseModel):
    recipient_username: str
    content: Optional[str] = None
    attachment_url: Optional[str] = None
    reply_to_id: Optional[int] = None

    @field_validator("content")
    @classmethod
    def content_len(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = strip_html_tags(v).strip()
        if len(v) > 2000:
            raise ValueError("Максимум 2000 символов")
        return v or None

    @model_validator(mode="after")
    def content_or_attachment(self):
        # An attachment-only message is valid (see Message.content's nullable=True) —
        # the empty-message rejection just needs to account for that second way of
        # saying something.
        if not self.content and not self.attachment_url:
            raise ValueError("Сообщение не может быть пустым")
        return self


def _reply_preview(reply_id: Optional[int], sender_username: Optional[str], content: Optional[str], attachment_url: Optional[str]) -> Optional[dict]:
    """Small quoted-preview payload for a message's reply_to. Takes plain already-resolved
    fields rather than a Message object/relationship on purpose: Message.reply_to is a
    self-referential lazy="selectin" relationship, and eager-loading it for a batch of
    rows (each of which would then also try to eager-load ITS OWN sender/recipient/
    reply_to) hit a real MissingGreenlet crash in SQLAlchemy's async loader, caught by
    this router's own test suite. messages_conversation() below bulk-fetches reply
    targets via an explicit SELECT instead of the relationship — sidesteps the
    recursive-eager-load path entirely and matches this codebase's established
    "batch-fetch related rows, don't rely on per-row relationship traversal" convention
    used elsewhere (e.g. GET /api/leaderboard's avatar_map). None both when the message
    isn't a reply and when the original was since deleted — Message.reply_to_id is
    ondelete="SET NULL" (though see that column's own docstring on why SQLite doesn't
    actually enforce it), so a deleted original just makes this None rather than a
    broken/dangling reference, since the bulk query below simply won't find that id."""
    if reply_id is None:
        return None
    return {
        "id": reply_id,
        "sender": sender_username,
        "content": content,
        "attachment_url": attachment_url,
    }


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

    attachment_url = body.attachment_url
    if attachment_url is not None:
        # Only ever accept a URL this same upload endpoint (POST /api/messages/attachment
        # above) just handed back — guards against a client passing an arbitrary URL
        # through as a "message attachment".
        prefix = "/api/uploads/dm/"
        if not attachment_url.startswith(prefix) or "/" in attachment_url[len(prefix):]:
            raise HTTPException(status_code=400, detail="Недопустимая ссылка на вложение")

    reply_msg = None
    if body.reply_to_id is not None:
        # A reply must point at a message that's actually part of this conversation
        # (either direction) — rejected outright (not degraded to a plain message)
        # since a client-supplied id pointing anywhere else would otherwise let one
        # user's reply UI leak a preview of someone else's unrelated DM.
        reply_res = await db.execute(
            select(Message).where(
                Message.id == body.reply_to_id,
                or_(
                    (Message.sender_id == current_user.id) & (Message.recipient_id == recipient.id),
                    (Message.sender_id == recipient.id) & (Message.recipient_id == current_user.id),
                ),
            )
        )
        reply_msg = reply_res.scalar_one_or_none()
        if reply_msg is None:
            raise HTTPException(status_code=404, detail="Сообщение для ответа не найдено")

    msg = Message(
        sender_id=current_user.id,
        recipient_id=recipient.id,
        content=body.content,
        attachment_url=attachment_url,
        reply_to_id=reply_msg.id if reply_msg else None,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    preview_text = msg.content[:100] if msg.content else "📷 Изображение"
    # DMs previously had no notification at all — the recipient only found out by
    # opening the inbox themselves. Same Notification mechanism as comment replies/
    # mentions, "message" type (see the notif-bell rendering in index.js/common.js).
    db.add(Notification(
        user_id=recipient.id,
        type="message",
        data=json.dumps({
            "from_username": current_user.username,
            "preview": preview_text,
        }, ensure_ascii=False),
    ))
    await db.commit()
    # Real-time push to the recipient's open /api/messages/stream connection(s), if
    # any — replaces index.js's old 5s poll of the open conversation. Payload mirrors
    # the Notification.data above; the client re-fetches on receipt rather than
    # trusting this raw payload as the source of truth (see dm_broadcast()'s docstring).
    dm_broadcast(recipient.id, {
        "from_username": current_user.username,
        "preview": preview_text,
    })
    asyncio.create_task(send_push(
        recipient.id,
        "Новое сообщение",
        f"{current_user.username}: {preview_text}",
        f"/?dm={current_user.username}",
    ))
    return {
        "id": msg.id,
        "sender": current_user.username,
        "recipient": recipient.username,
        "content": msg.content,
        "attachment_url": msg.attachment_url,
        # reply_msg (if present) was loaded a few lines up via a plain select(Message)
        # query, never through the reply_to relationship — safe to access .sender.username
        # directly here, unlike messages_conversation() below (see _reply_preview()'s
        # docstring for why that one can't do the same for a batch of rows).
        "reply_to": _reply_preview(reply_msg.id, reply_msg.sender.username, reply_msg.content, reply_msg.attachment_url) if reply_msg else None,
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
                "attachment_url": last_msg.attachment_url,
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

    # Bulk-fetch reply_to targets in one query (id/sender-username/content/attachment_url
    # only, joined to User for the username) instead of the reply_to relationship — see
    # _reply_preview()'s docstring for why that relationship can't be eager-loaded for a
    # batch of rows without hitting a real async-loader crash.
    reply_ids = {m.reply_to_id for m in messages if m.reply_to_id is not None}
    reply_map: dict[int, tuple] = {}
    if reply_ids:
        reply_rows = (await db.execute(
            select(Message.id, User.username, Message.content, Message.attachment_url)
            .join(User, User.id == Message.sender_id)
            .where(Message.id.in_(reply_ids))
        )).all()
        reply_map = {row[0]: row for row in reply_rows}

    return {
        "partner": {"id": partner.id, "username": partner.username, "avatar_url": partner.avatar_url},
        "has_more": has_more,
        "messages": [
            {
                "id": m.id,
                "sender": m.sender.username,
                "content": m.content,
                "attachment_url": m.attachment_url,
                "reply_to": _reply_preview(*reply_map[m.reply_to_id]) if m.reply_to_id in reply_map else None,
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
