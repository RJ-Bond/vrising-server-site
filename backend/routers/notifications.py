import json

from pydantic import BaseModel
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from ..database import get_db
from ..models import User, Notification, PushSubscription
from ..auth import get_current_user
from ..helpers import VAPID_PUBLIC_KEY

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


# ─── Web Push subscriptions ───────────────────────────────────────────────────
# Separate from the Notification rows above: a user may have zero, one, or several
# PushSubscription rows (one per browser/device that opted in — see PushSubscription in
# models.py). send_push() in helpers.py fans out to all of a user's rows and is called
# alongside every Notification(...) creation across backend/routers/*.py, never instead
# of it.

class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeBody(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys


class PushUnsubscribeBody(BaseModel):
    endpoint: str


@router.get("/api/push/vapid-public-key")
async def get_vapid_public_key():
    """Public half of the server's VAPID keypair, used as `applicationServerKey` for
    `pushManager.subscribe()`. Empty string means push isn't configured on this
    deployment (VAPID_PRIVATE_KEY unset server-side) — the frontend treats that as
    "feature unavailable" and hides the opt-in control rather than letting a subscribe
    attempt fail."""
    return {"public_key": VAPID_PUBLIC_KEY}


@router.post("/api/push/subscribe")
async def push_subscribe(
    body: PushSubscribeBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upserts by endpoint (unique per browser+device+site — see the unique constraint
    on PushSubscription.endpoint) rather than by user, so re-subscribing on the same
    device (e.g. the browser silently rotated the push endpoint) updates the existing
    row instead of hitting the unique constraint — and re-subscribing under a
    *different* account on a shared device correctly re-points that device's
    subscription at the new user rather than leaving two rows racing for it."""
    res = await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    sub = res.scalar_one_or_none()
    if sub is None:
        db.add(PushSubscription(
            user_id=current_user.id, endpoint=body.endpoint,
            p256dh=body.keys.p256dh, auth=body.keys.auth,
        ))
    else:
        sub.user_id = current_user.id
        sub.p256dh = body.keys.p256dh
        sub.auth = body.keys.auth
    await db.commit()
    return {"ok": True}


@router.delete("/api/push/subscribe")
async def push_unsubscribe(
    body: PushUnsubscribeBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Scoped to the caller's own user_id — matches DELETE /api/notifications/{id}'s
    "silent no-op if it's not yours" shape, so this never leaks whether some other
    account holds that endpoint."""
    res = await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint, PushSubscription.user_id == current_user.id)
    )
    sub = res.scalar_one_or_none()
    if sub is not None:
        await db.delete(sub)
        await db.commit()
    return {"ok": True}
