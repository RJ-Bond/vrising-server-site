import json

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from ..database import get_db
from ..models import User, Notification
from ..auth import get_current_user

router = APIRouter()


# ─── Notifications ────────────────────────────────────────────────────────────

@router.get("/api/notifications")
async def get_notifications(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    )
    items = res.scalars().all()
    unread = sum(1 for n in items if not n.read)
    return {
        "items": [
            {"id": n.id, "type": n.type, "data": json.loads(n.data or "{}"), "read": n.read, "created_at": n.created_at.isoformat()}
            for n in items
        ],
        "unread": unread
    }


@router.post("/api/notifications/read-all")
async def mark_notifications_read(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.read == False)
        .values(read=True)
    )
    await db.commit()
    return {"ok": True}


@router.delete("/api/notifications/{notif_id}")
async def delete_notification(notif_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    n = await db.get(Notification, notif_id)
    if n and n.user_id == current_user.id:
        await db.delete(n)
        await db.commit()
    return {"ok": True}
