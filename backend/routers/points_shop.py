import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, update, delete

from ..database import get_db
from ..models import User, PointsTransaction, ShopItem, ShopRedemption, ShopWishlistItem, Notification, PlayerRecord
from ..auth import get_admin_user, get_current_user
from ..helpers import _audit, _award_points, _fmt_dt, _get_points_config, activity_broadcast, send_push
from ..rate_limit import limiter
from ..schemas import (
    ShopItemCreate,
    ShopItemUpdate,
    ShopItemOut,
    ShopRedeemIn,
    ShopRedemptionResolveIn,
    ShopRedemptionOut,
    ShopWishlistStatusOut,
    PointsGrantIn,
    PointsGrantBulkIn,
    PointsGrantBulkEntryResult,
    PointsGrantBulkOut,
    PointsTransactionOut,
)

router = APIRouter()


# ─── Points economy — shop catalog (admin) ─────────────────────────────────────
# Mirrors the Announcements CRUD pattern immediately above: XCreate/XUpdate all-Optional
# + exclude_unset/setattr, XOut with from_attributes, _audit() on every mutation.

@router.get("/api/admin/shop/items", response_model=list[ShopItemOut])
async def list_shop_items_admin(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Unlike GET /api/shop/items (public), this returns every item including inactive
    ones — the admin catalog table needs to show/toggle them."""
    result = await db.execute(select(ShopItem).order_by(ShopItem.sort_order, ShopItem.id))
    return [ShopItemOut.model_validate(i) for i in result.scalars().all()]


@router.post("/api/admin/shop/items", response_model=ShopItemOut, status_code=201)
async def create_shop_item(
    body: ShopItemCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    item = ShopItem(
        name=body.name, description=body.description, cost=body.cost,
        image_url=body.image_url, is_active=body.is_active, stock=body.stock,
        sort_order=body.sort_order, category=body.category,
        weekly_limit_per_user=body.weekly_limit_per_user,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    await _audit(db, current_user.id, "shop.item.create", target_type="shop_item", target_id=item.id, detail=item.name)
    await db.commit()
    return ShopItemOut.model_validate(item)


@router.put("/api/admin/shop/items/{item_id}", response_model=ShopItemOut)
async def update_shop_item(
    item_id: int,
    body: ShopItemUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    item = (await db.execute(select(ShopItem).where(ShopItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await _audit(db, current_user.id, "shop.item.update", target_type="shop_item", target_id=item.id, detail=item.name)
    await db.commit()
    await db.refresh(item)
    return ShopItemOut.model_validate(item)


@router.delete("/api/admin/shop/items/{item_id}", status_code=204)
async def delete_shop_item(
    item_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """shop_item_id is declared ON DELETE SET NULL on ShopRedemption (past redemption
    history — item_name_snapshot/cost_snapshot — survives a catalog item being removed)
    and ON DELETE CASCADE on ShopWishlistItem. Neither is actually enforced by the live
    DB though: SQLite only applies ON DELETE behavior on connections that have run
    `PRAGMA foreign_keys = ON`, and this app's engine (backend/database.py) never sets
    that — same caveat as GameClanMember.clan_id's docstring. So the wishlist cleanup
    below is done explicitly rather than relied upon; ShopRedemption.shop_item_id is
    left as-is (nothing joins back through it, so a dangling id is harmless there)."""
    item = (await db.execute(select(ShopItem).where(ShopItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")
    await db.execute(delete(ShopWishlistItem).where(ShopWishlistItem.shop_item_id == item_id))
    await _audit(db, current_user.id, "shop.item.delete", target_type="shop_item", target_id=item.id, detail=item.name)
    await db.delete(item)
    await db.commit()


# ─── Points economy — redemption queue (admin) ─────────────────────────────────
# Purchase requests fulfilled MANUALLY in-game by an admin (v1 — see delivery_mode on
# ShopRedemption / the module docstring at the top of models.py's ShopRedemption class).

@router.get("/api/admin/shop/redemptions")
async def list_shop_redemptions_admin(
    status: str = Query(default="pending"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    q: str = Query(""),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Default filter is "pending" (the actionable queue); pass status="" for every
    status. Same page/per_page pagination convention as GET /api/admin/audit-log."""
    filters = []
    if status.strip():
        filters.append(ShopRedemption.status == status.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(User.username.ilike(like), ShopRedemption.item_name_snapshot.ilike(like)))
    base_query = select(ShopRedemption, User.username).join(User, User.id == ShopRedemption.user_id).where(*filters)
    count_query = select(func.count(ShopRedemption.id)).join(User, User.id == ShopRedemption.user_id).where(*filters)
    total = (await db.execute(count_query)).scalar_one()
    rows = (await db.execute(
        base_query.order_by(ShopRedemption.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    )).all()
    items = []
    for r, username in rows:
        out = ShopRedemptionOut.model_validate(r)
        out.username = username
        items.append(out)

    # Weekly-limit context (see ShopRedemptionOut.weekly_limit_per_user's docstring) —
    # one bulk lookup of the current per-item limits, then one bulk grouped count of
    # this page's users' recent redemptions, instead of a per-row query. Only items
    # actually referenced on this page are looked up, so the extra cost stays
    # proportional to page size (<=200) regardless of total catalog/redemption size.
    item_ids = {r.shop_item_id for r, _ in rows if r.shop_item_id is not None}
    if item_ids:
        limit_rows = (await db.execute(
            select(ShopItem.id, ShopItem.weekly_limit_per_user).where(ShopItem.id.in_(item_ids))
        )).all()
        limits_by_item = {iid: lim for iid, lim in limit_rows if lim is not None}
        if limits_by_item:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            count_rows = (await db.execute(
                select(ShopRedemption.user_id, ShopRedemption.shop_item_id, func.count(ShopRedemption.id))
                .where(
                    ShopRedemption.shop_item_id.in_(limits_by_item.keys()),
                    ShopRedemption.status != "cancelled",
                    ShopRedemption.created_at >= cutoff,
                )
                .group_by(ShopRedemption.user_id, ShopRedemption.shop_item_id)
            )).all()
            used_by_pair = {(uid, iid): cnt for uid, iid, cnt in count_rows}
            for (r, _), out in zip(rows, items, strict=True):
                if r.shop_item_id in limits_by_item:
                    out.weekly_limit_per_user = limits_by_item[r.shop_item_id]
                    out.weekly_used = used_by_pair.get((r.user_id, r.shop_item_id), 0)

    return {"total": total, "page": page, "per_page": per_page, "items": items}


@router.post("/api/admin/shop/redemptions/{redemption_id}/fulfill", response_model=ShopRedemptionOut)
async def fulfill_shop_redemption(
    redemption_id: int,
    body: ShopRedemptionResolveIn = Body(default_factory=ShopRedemptionResolveIn),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    r = (await db.execute(select(ShopRedemption).where(ShopRedemption.id == redemption_id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Redemption not found")
    if r.status != "pending":
        raise HTTPException(409, "Redemption is not pending")
    r.status = "fulfilled"
    r.resolved_at = datetime.now(timezone.utc)
    r.resolved_by = current_user.username
    if body.admin_note:
        r.admin_note = body.admin_note
    await _audit(db, current_user.id, "shop.redemption.fulfill", target_type="shop_redemption", target_id=r.id, detail=r.item_name_snapshot)
    db.add(Notification(
        user_id=r.user_id, type="shop_fulfilled",
        data=json.dumps({"item_name": r.item_name_snapshot, "redemption_id": r.id}, ensure_ascii=False),
    ))
    await db.commit()
    await db.refresh(r)
    asyncio.create_task(send_push(
        r.user_id,
        "Заявка выполнена",
        f"«{r.item_name_snapshot}» выполнена",
        "/profile.html",
    ))
    redeemer = (await db.execute(select(User.username).where(User.id == r.user_id))).scalar_one_or_none()
    if redeemer:
        activity_broadcast({
            "type": "shop_redemption", "title": r.item_name_snapshot, "subtitle": f"Получил: {redeemer}",
            "url": "/shop.html", "icon": "🛒", "timestamp": _fmt_dt(r.resolved_at or r.created_at),
        })
    return ShopRedemptionOut.model_validate(r)


@router.post("/api/admin/shop/redemptions/{redemption_id}/cancel", response_model=ShopRedemptionOut)
async def cancel_shop_redemption(
    redemption_id: int,
    body: ShopRedemptionResolveIn = Body(default_factory=ShopRedemptionResolveIn),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Refunds the points spent, and 409s if the redemption isn't currently pending — this
    is what prevents a double-refund from two admins (or one admin double-clicking)
    cancelling the same already-cancelled/fulfilled request."""
    r = (await db.execute(select(ShopRedemption).where(ShopRedemption.id == redemption_id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Redemption not found")
    if r.status != "pending":
        raise HTTPException(409, "Redemption is not pending")
    user_res = await db.execute(select(User).where(User.id == r.user_id))
    user = user_res.scalar_one_or_none()
    if user is not None:
        await _award_points(db, user, r.cost_snapshot, "refund", f"cancelled redemption #{r.id}: {r.item_name_snapshot}")
    r.status = "cancelled"
    r.resolved_at = datetime.now(timezone.utc)
    r.resolved_by = current_user.username
    if body.admin_note:
        r.admin_note = body.admin_note
    await _audit(db, current_user.id, "shop.redemption.cancel", target_type="shop_redemption", target_id=r.id, detail=r.item_name_snapshot)
    if user is not None:
        db.add(Notification(
            user_id=user.id, type="shop_cancelled",
            data=json.dumps({"item_name": r.item_name_snapshot, "redemption_id": r.id, "refund": r.cost_snapshot}, ensure_ascii=False),
        ))
    await db.commit()
    await db.refresh(r)
    if user is not None:
        asyncio.create_task(send_push(
            user.id,
            "Заявка отменена",
            f"«{r.item_name_snapshot}» отменена, {r.cost_snapshot} очков возвращено",
            "/profile.html",
        ))
    return ShopRedemptionOut.model_validate(r)


# ─── Points economy — manual grants & ledger (admin) ───────────────────────────

@router.post("/api/admin/points/grant", response_model=PointsTransactionOut, status_code=201)
async def grant_points(
    body: PointsGrantIn,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Manual balance adjustment — primarily for donations, since no payment integration
    exists in this repo yet (see the module note at the top of models.py's
    PointsTransaction class). delta may be negative for corrections."""
    user_res = await db.execute(select(User).where(User.id == body.user_id))
    user = user_res.scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "User not found")
    reason = (body.reason or "").strip()[:32] or "admin_adjust"
    await _award_points(db, user, body.delta, reason, body.note)
    await _audit(db, current_user.id, "points.grant", target_type="user", target_id=user.id, detail=f"{body.delta:+d} ({reason}): {body.note or ''}")
    db.add(Notification(
        user_id=user.id, type="points_grant",
        data=json.dumps({"delta": body.delta, "reason": reason, "note": body.note or ""}, ensure_ascii=False),
    ))
    await db.commit()
    asyncio.create_task(send_push(
        user.id,
        "Начислены очки",
        f"{body.delta:+d} очков" + (f": {body.note}" if body.note else ""),
        "/profile.html",
    ))
    tx_res = await db.execute(
        select(PointsTransaction).where(PointsTransaction.user_id == user.id).order_by(PointsTransaction.id.desc()).limit(1)
    )
    tx = tx_res.scalar_one()
    out = PointsTransactionOut.model_validate(tx)
    out.username = user.username
    return out


@router.post("/api/admin/points/grant-bulk", response_model=PointsGrantBulkOut)
async def grant_points_bulk(
    body: PointsGrantBulkIn,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk variant of POST /api/admin/points/grant — one shared delta/reason/note
    applied to every identifier in body.identifiers, each resolved against EITHER
    User.username OR User.steam_id (see PointsGrantBulkIn's docstring for why). Reuses
    the exact same _award_points ledger helper the single-grant endpoint uses — no
    duplicated balance/transaction logic. Deliberately per-entry success/failure rather
    than one all-or-nothing transaction: an admin pasting a list of 50 names shouldn't
    lose the other 49 valid grants because of one typo'd username, same reasoning as
    POST /api/admin/users/bulk's per-row filtering in routers/users.py."""
    reason = (body.reason or "").strip()[:32] or "admin_adjust"
    results: list[PointsGrantBulkEntryResult] = []
    for identifier in body.identifiers:
        user_res = await db.execute(
            select(User).where(or_(User.username == identifier, User.steam_id == identifier))
        )
        user = user_res.scalars().first()
        if user is None:
            results.append(PointsGrantBulkEntryResult(
                identifier=identifier, success=False, error="Пользователь не найден",
            ))
            continue
        await _award_points(db, user, body.delta, reason, body.note)
        await _audit(
            db, current_user.id, "points.grant", target_type="user", target_id=user.id,
            detail=f"bulk {body.delta:+d} ({reason}): {body.note or ''}",
        )
        db.add(Notification(
            user_id=user.id, type="points_grant",
            data=json.dumps({"delta": body.delta, "reason": reason, "note": body.note or ""}, ensure_ascii=False),
        ))
        results.append(PointsGrantBulkEntryResult(
            identifier=identifier, success=True, user_id=user.id, username=user.username,
            balance_after=user.points_balance,
        ))
    await db.commit()
    for r in results:
        if r.success:
            asyncio.create_task(send_push(
                r.user_id,
                "Начислены очки",
                f"{body.delta:+d} очков" + (f": {body.note}" if body.note else ""),
                "/profile.html",
            ))
    succeeded = sum(1 for r in results if r.success)
    return PointsGrantBulkOut(results=results, succeeded=succeeded, failed=len(results) - succeeded)


@router.get("/api/admin/points/transactions")
async def list_points_transactions_admin(
    user_id: Optional[int] = Query(default=None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Full ledger, optionally filtered to one player — the site-wide audit trail behind
    every balance change (earn, spend, grant, refund)."""
    filters = []
    if user_id is not None:
        filters.append(PointsTransaction.user_id == user_id)
    total = (await db.execute(select(func.count(PointsTransaction.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(PointsTransaction, User.username).join(User, User.id == PointsTransaction.user_id)
        .where(*filters).order_by(PointsTransaction.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).all()
    items = []
    for tx, username in rows:
        out = PointsTransactionOut.model_validate(tx)
        out.username = username
        items.append(out)
    return {"total": total, "page": page, "per_page": per_page, "items": items}


# ─── Points economy — anomaly detection (admin) ────────────────────────────────
# Individual playtime award implying more than this many real minutes in one session
# (12h) — a single POST /api/plugin/sessions report this large is either a plugin/clock
# bug or a forged request against a leaked X-Plugin-Key, not a real play session.
_ANOMALY_LARGE_SESSION_MINUTES = 720
# Same signal aggregated per user per calendar day (24h) — catches the same abuse
# spread across several smaller session reports instead of one big one.
_ANOMALY_IMPOSSIBLE_DAILY_MINUTES = 1440
# Playtime-award transactions for one user within a single clock-hour bucket. Each
# award fires once per disconnect (see plugin_integration.py's POST
# /api/plugin/sessions) — normal play produces at most a handful of reconnects per
# hour, so a sustained burst above this points at automated reconnect-spam farming
# rather than a person actually playing.
_ANOMALY_BURST_TX_PER_HOUR = 8


@router.get("/api/admin/points/anomalies")
async def points_anomalies(
    days: int = Query(default=7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Extension of GET /api/admin/points/diagnostics below, kept as its own endpoint
    (separate admin-panel card) rather than merged into that response — diagnostics
    answers "is earning working at all", this answers "does anything about HOW it's
    earning look like abuse", a different question with a different audience/urgency.

    Three SQL-aggregate checks over PointsTransaction (reason="playtime"), all scoped
    to the trailing `days` days:
      1. large_single_sessions — one award implying a single session longer than
         _ANOMALY_LARGE_SESSION_MINUTES.
      2. impossible_daily_rate — one user's playtime awards on one calendar day
         implying more real playtime than the day has hours for.
      3. burst_activity — one user racking up an unusually high count of separate
         playtime-award transactions within a single hour.

    Deliberately does NOT attempt "same Steam ID or IP linked to many accounts":
    User.steam_id is DB-unique (models.py — one site account per SteamID, so that
    vector is structurally impossible under the current schema) and no IP address is
    persisted against a User anywhere (registration/login never store one) — surfacing
    that would need a real schema change + migration, out of scope here."""
    points_cfg = await _get_points_config(db)
    per_minute = points_cfg["per_minute"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    large_sessions: list[dict] = []
    impossible_daily_rate: list[dict] = []
    if per_minute > 0:
        # 1. Individual outlier awards.
        outlier_rows = (await db.execute(
            select(PointsTransaction, User.username)
            .join(User, User.id == PointsTransaction.user_id)
            .where(
                PointsTransaction.reason == "playtime",
                PointsTransaction.created_at >= cutoff,
                PointsTransaction.delta >= _ANOMALY_LARGE_SESSION_MINUTES * per_minute,
            )
            .order_by(PointsTransaction.delta.desc())
            .limit(50)
        )).all()
        large_sessions = [
            {
                "user_id": tx.user_id, "username": username,
                "created_at": _fmt_dt(tx.created_at),
                "implied_minutes": tx.delta // per_minute,
                "awarded_points": tx.delta,
                "detail": tx.detail,
            }
            for tx, username in outlier_rows
        ]

        # 2. Same signal, aggregated per user per calendar day (catches session-splitting).
        day_rows = (await db.execute(
            select(
                PointsTransaction.user_id, User.username,
                func.date(PointsTransaction.created_at).label("day"),
                func.sum(PointsTransaction.delta).label("total"),
            )
            .join(User, User.id == PointsTransaction.user_id)
            .where(PointsTransaction.reason == "playtime", PointsTransaction.created_at >= cutoff)
            .group_by(PointsTransaction.user_id, User.username, func.date(PointsTransaction.created_at))
            .having(func.sum(PointsTransaction.delta) >= _ANOMALY_IMPOSSIBLE_DAILY_MINUTES * per_minute)
            .order_by(func.sum(PointsTransaction.delta).desc())
            .limit(50)
        )).all()
        impossible_daily_rate = [
            {
                "user_id": user_id, "username": username, "date": day,
                "implied_minutes": total // per_minute, "awarded_points": total,
            }
            for user_id, username, day, total in day_rows
        ]

    # 3. Burst frequency — a pure transaction-count signal, independent of per_minute
    # (still meaningful even if the earning rate has since been set to 0).
    burst_rows = (await db.execute(
        select(
            PointsTransaction.user_id, User.username,
            func.strftime("%Y-%m-%d %H", PointsTransaction.created_at).label("hour"),
            func.count(PointsTransaction.id).label("cnt"),
        )
        .join(User, User.id == PointsTransaction.user_id)
        .where(PointsTransaction.reason == "playtime", PointsTransaction.created_at >= cutoff)
        .group_by(PointsTransaction.user_id, User.username, func.strftime("%Y-%m-%d %H", PointsTransaction.created_at))
        .having(func.count(PointsTransaction.id) >= _ANOMALY_BURST_TX_PER_HOUR)
        .order_by(func.count(PointsTransaction.id).desc())
        .limit(50)
    )).all()
    burst_activity = [
        {"user_id": user_id, "username": username, "hour": hour, "tx_count": cnt}
        for user_id, username, hour, cnt in burst_rows
    ]

    return {
        "window_days": days,
        "large_single_sessions": large_sessions,
        "impossible_daily_rate": impossible_daily_rate,
        "burst_activity": burst_activity,
        "anomaly_count": len(large_sessions) + len(impossible_daily_rate) + len(burst_activity),
    }


@router.get("/api/admin/points/diagnostics")
async def points_diagnostics(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Answers "is automatic point-earning actually working?" without needing direct DB
    access — built after a support question where playtime/streak points appeared to
    have stopped and the two most likely causes (unlinked accounts, and the earning
    hooks not firing at all) weren't visible anywhere in the admin panel. Both
    automatic earn paths (POST /api/plugin/sessions "playtime", POST
    /api/plugin/connect-streak "streak" — see plugin_integration.py) silently no-op
    when PlayerRecord.steam_id doesn't match any User.steam_id, which looks identical
    to "the mechanism is broken" from the admin's side unless linked-vs-unlinked is
    surfaced explicitly."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    active_total = (await db.execute(
        select(func.count(func.distinct(PlayerRecord.steam_id)))
        .where(PlayerRecord.steam_id.isnot(None), PlayerRecord.last_seen >= cutoff)
    )).scalar_one()
    active_linked = (await db.execute(
        select(func.count(func.distinct(PlayerRecord.steam_id)))
        .select_from(PlayerRecord)
        .join(User, User.steam_id == PlayerRecord.steam_id)
        .where(PlayerRecord.steam_id.isnot(None), PlayerRecord.last_seen >= cutoff)
    )).scalar_one()

    def _reason_stats(reason: str):
        return (
            select(func.count(PointsTransaction.id), func.max(PointsTransaction.created_at))
            .where(PointsTransaction.reason == reason)
        )

    playtime_count, playtime_last = (await db.execute(_reason_stats("playtime"))).one()
    streak_count, streak_last = (await db.execute(_reason_stats("streak"))).one()
    playtime_recent = (await db.execute(
        select(func.count(PointsTransaction.id))
        .where(PointsTransaction.reason == "playtime", PointsTransaction.created_at >= week_cutoff)
    )).scalar_one()
    streak_recent = (await db.execute(
        select(func.count(PointsTransaction.id))
        .where(PointsTransaction.reason == "streak", PointsTransaction.created_at >= week_cutoff)
    )).scalar_one()

    points_cfg = await _get_points_config(db)

    return {
        # Players seen in-game in the last 14 days (distinct PlayerRecord.steam_id) vs.
        # how many of those steam_ids match a registered User — the gap is exactly the
        # set of active players earning zero points regardless of settings, because
        # both award hooks require a linked account to have anyone to credit.
        "active_players_14d": active_total,
        "active_players_linked_14d": active_linked,
        "active_players_unlinked_14d": active_total - active_linked,
        # Ever, and in the last 7 days — a zero "recent" count with a non-zero "ever"
        # count points at the mechanism having stopped recently (config/plugin issue);
        # zero for both, despite active+linked players, points at something else
        # entirely (e.g. the two Settings rows below saved as 0).
        "playtime_awards_total": playtime_count,
        "playtime_awards_last_7d": playtime_recent,
        "playtime_awards_last_at": _fmt_dt(playtime_last) if playtime_last else None,
        "streak_awards_total": streak_count,
        "streak_awards_last_7d": streak_recent,
        "streak_awards_last_at": _fmt_dt(streak_last) if streak_last else None,
        "points_per_minute_playtime": points_cfg["per_minute"],
        "points_streak_bonus": points_cfg["streak_bonus"],
        "points_streak_min_days": points_cfg["streak_min_days"],
    }


# ─── Points economy — shop (player-facing) ─────────────────────────────────────

async def _shop_items_with_user_state(db: AsyncSession, items: list[ShopItem], user_id: int) -> list[ShopItemOut]:
    """Shared annotation step for GET /api/shop/items and GET /api/shop/wishlist/me —
    stamps each ShopItemOut with this user's wishlist state and, for items carrying a
    weekly_limit_per_user, how many redemptions they have left in the trailing 7 days
    (same window/exclusion rule as the 409 check in POST /api/shop/redeem below —
    cancelled redemptions don't count against the limit)."""
    if not items:
        return []
    wishlist_ids = set((await db.execute(
        select(ShopWishlistItem.shop_item_id).where(ShopWishlistItem.user_id == user_id)
    )).scalars().all())
    limited_ids = [i.id for i in items if i.weekly_limit_per_user is not None]
    counts: dict[int, int] = {}
    if limited_ids:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        rows = (await db.execute(
            select(ShopRedemption.shop_item_id, func.count(ShopRedemption.id))
            .where(
                ShopRedemption.user_id == user_id,
                ShopRedemption.shop_item_id.in_(limited_ids),
                ShopRedemption.status != "cancelled",
                ShopRedemption.created_at >= cutoff,
            )
            .group_by(ShopRedemption.shop_item_id)
        )).all()
        counts = dict(rows)
    out = []
    for i in items:
        o = ShopItemOut.model_validate(i)
        o.wishlisted = i.id in wishlist_ids
        if i.weekly_limit_per_user is not None:
            o.weekly_remaining = max(0, i.weekly_limit_per_user - counts.get(i.id, 0))
        out.append(o)
    return out


@router.get("/api/shop/items", response_model=list[ShopItemOut])
async def list_shop_items_public(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Active items only. stock is included but NOT filtered out at stock=0 — the
    front-end greys those out instead of hiding them, so a player can still see what
    exists even when temporarily out of stock."""
    result = await db.execute(select(ShopItem).where(ShopItem.is_active == True).order_by(ShopItem.sort_order, ShopItem.id))
    items = result.scalars().all()
    return await _shop_items_with_user_state(db, items, current_user.id)


@router.post("/api/shop/wishlist/{item_id}", response_model=ShopWishlistStatusOut, status_code=201)
async def add_shop_wishlist_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Idempotent add — wishlisting an item that's already saved is a success no-op, not
    an error, so a double-click (or the heart icon re-firing before its state updates)
    never surfaces an error toast."""
    exists = (await db.execute(select(ShopItem.id).where(ShopItem.id == item_id))).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404, "Item not found")
    already = (await db.execute(
        select(ShopWishlistItem.id).where(ShopWishlistItem.user_id == current_user.id, ShopWishlistItem.shop_item_id == item_id)
    )).scalar_one_or_none()
    if already is None:
        db.add(ShopWishlistItem(user_id=current_user.id, shop_item_id=item_id))
        await db.commit()
    return ShopWishlistStatusOut(shop_item_id=item_id, wishlisted=True)


@router.delete("/api/shop/wishlist/{item_id}", response_model=ShopWishlistStatusOut)
async def remove_shop_wishlist_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Idempotent remove — removing an item that was never (or no longer) wishlisted is
    also a success no-op, same reasoning as the add endpoint above."""
    await db.execute(
        delete(ShopWishlistItem).where(ShopWishlistItem.user_id == current_user.id, ShopWishlistItem.shop_item_id == item_id)
    )
    await db.commit()
    return ShopWishlistStatusOut(shop_item_id=item_id, wishlisted=False)


@router.get("/api/shop/wishlist/me", response_model=list[ShopItemOut])
async def my_shop_wishlist(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Same ShopItemOut shape as GET /api/shop/items (via the shared annotation helper
    above) so shop.html can reuse its existing card-render function unchanged. Includes
    inactive items too — unlike the public catalog, a player's own saved list shouldn't
    silently drop something they favourited just because an admin toggled it off."""
    rows = (await db.execute(
        select(ShopItem).join(ShopWishlistItem, ShopWishlistItem.shop_item_id == ShopItem.id)
        .where(ShopWishlistItem.user_id == current_user.id)
        .order_by(ShopItem.sort_order, ShopItem.id)
    )).scalars().all()
    return await _shop_items_with_user_state(db, rows, current_user.id)


@router.post("/api/shop/redeem", response_model=ShopRedemptionOut, status_code=201)
@limiter.limit("10/minute")
async def redeem_shop_item(
    request: Request,
    body: ShopRedeemIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SQLite-specific correctness note (this repo's engine has no row-locking —
    backend/database.py's plain create_async_engine, no with_for_update() anywhere in the
    codebase): a naive "read balance in Python, check, then UPDATE" has a race window
    between two concurrent requests for the same user. A single conditional UPDATE is used
    instead — the WHERE clause re-checks the balance as one indivisible SQL statement, so
    at most one of two concurrent double-redeem attempts can ever succeed. Same pattern for
    stock. See backend/tests/test_points_shop.py's asyncio.gather concurrency test.

    weekly_limit_per_user has no UPDATE to piggyback the same trick on (it's a COUNT over
    ShopRedemption, not a single row), so it's checked twice: once here, up front, as a
    cheap best-effort rejection that avoids doing the balance/stock work at all in the
    common (not racing) already-over-limit case, and again below, authoritatively, right
    after the balance UPDATE. That UPDATE is this transaction's first write, so on SQLite
    it's also the point this connection acquires the single writer lock the engine's whole
    concurrency model serializes on (see database.py's WAL comment) — a concurrent
    request's redemption that already committed is guaranteed visible to the recount below
    once this transaction holds that lock, closing the window this first, merely-optimistic
    check can't (see test_concurrent_double_redeem_respects_weekly_limit, which fails
    against this first check alone)."""
    item_res = await db.execute(select(ShopItem).where(ShopItem.id == body.shop_item_id))
    item = item_res.scalar_one_or_none()
    if item is None or not item.is_active:
        raise HTTPException(404, "Item not found")

    async def _recent_redemption_count() -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        return (await db.execute(
            select(func.count(ShopRedemption.id)).where(
                ShopRedemption.user_id == current_user.id,
                ShopRedemption.shop_item_id == item.id,
                ShopRedemption.status != "cancelled",
                ShopRedemption.created_at >= cutoff,
            )
        )).scalar_one()

    if item.weekly_limit_per_user is not None:
        if (await _recent_redemption_count()) >= item.weekly_limit_per_user:
            raise HTTPException(409, "Weekly limit for this item reached — try again later")

    result = await db.execute(
        update(User).where(User.id == current_user.id, User.points_balance >= item.cost)
        .values(points_balance=User.points_balance - item.cost)
    )
    if result.rowcount == 0:
        await db.rollback()
        raise HTTPException(400, "Insufficient points balance")

    if item.stock is not None:
        stock_result = await db.execute(
            update(ShopItem).where(ShopItem.id == item.id, ShopItem.stock > 0)
            .values(stock=ShopItem.stock - 1)
        )
        if stock_result.rowcount == 0:
            await db.rollback()
            raise HTTPException(409, "Item out of stock")

    # Authoritative re-check — see docstring above for why this, unlike the identical
    # check up front, actually closes the race.
    if item.weekly_limit_per_user is not None:
        if (await _recent_redemption_count()) >= item.weekly_limit_per_user:
            await db.rollback()
            raise HTTPException(409, "Weekly limit for this item reached — try again later")

    # Re-fetch the fresh balance for the ledger snapshot — current_user.points_balance in
    # memory reflects the pre-request state, not what the conditional UPDATE above (or any
    # concurrent request that also just succeeded) actually left it at.
    fresh_res = await db.execute(select(User.points_balance).where(User.id == current_user.id))
    fresh_balance = fresh_res.scalar_one()

    redemption = ShopRedemption(
        user_id=current_user.id, shop_item_id=item.id,
        item_name_snapshot=item.name, cost_snapshot=item.cost,
        status="pending", delivery_mode="manual", player_note=body.note,
    )
    db.add(redemption)
    await db.flush()  # assign redemption.id for the ledger row's ref_id, before commit

    db.add(PointsTransaction(
        user_id=current_user.id, delta=-item.cost, balance_after=fresh_balance,
        reason="redeem", detail=item.name[:256], ref_type="shop_redemption", ref_id=redemption.id,
    ))
    await db.commit()
    await db.refresh(redemption)
    return ShopRedemptionOut.model_validate(redemption)


@router.get("/api/shop/redemptions/me")
async def my_shop_redemptions(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filters = [ShopRedemption.user_id == current_user.id]
    total = (await db.execute(select(func.count(ShopRedemption.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(ShopRedemption).where(*filters).order_by(ShopRedemption.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {"total": total, "page": page, "per_page": per_page, "items": [ShopRedemptionOut.model_validate(r) for r in rows]}


# ─── Points economy — own-ledger type filter (player-facing) ──────────────────
# Reason strings actually written by _award_points()/the redeem path today (grep across
# plugin_integration.py, points_shop.py, users.py, helpers.py): "playtime", "streak",
# "redeem", "refund", "nickname_change" are the five fixed ones; POST /api/admin/points/
# grant(-bulk) additionally accepts a free-text reason from the admin (up to 32 chars,
# "donation"/"admin_adjust" are just its two shipped defaults, nothing enforces those
# specific strings). Bucketed into 4 user-facing types for the Points tab's filter
# dropdown — anything not in the explicit lists below (i.e. any admin-typed grant
# reason) falls into "gifted" as a catch-all, since a manual balance adjustment is
# definitionally "someone gave/took you points", not automatic earning or a
# player-initiated spend.
_POINTS_TX_TYPE_REASONS = {
    "earned": ("playtime", "streak"),
    "spent": ("redeem", "nickname_change"),
    "refund": ("refund",),
}
_POINTS_TX_KNOWN_NON_GIFTED_REASONS = tuple(r for reasons in _POINTS_TX_TYPE_REASONS.values() for r in reasons)


def _points_tx_type_filter(type_: str):
    """SQLAlchemy filter expression for the `type` param shared by GET /api/points/
    transactions/me and GET /api/points/transactions/export. Raises 400 on an unknown
    value rather than silently ignoring it (matching e.g. PUT /api/profile/badge-style's
    validation style)."""
    if type_ in _POINTS_TX_TYPE_REASONS:
        return PointsTransaction.reason.in_(_POINTS_TX_TYPE_REASONS[type_])
    if type_ == "gifted":
        return PointsTransaction.reason.notin_(_POINTS_TX_KNOWN_NON_GIFTED_REASONS)
    raise HTTPException(400, "Unknown type filter — use earned, spent, refund, or gifted")


@router.get("/api/points/transactions/me")
async def my_points_transactions(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    tx_type: Optional[str] = Query(default=None, alias="type", description="earned | spent | refund | gifted"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Caller's own full ledger — earn rows (playtime/streak) as well as spend/refund.
    Optional `type` narrows to one bucket, see _points_tx_type_filter()."""
    filters = [PointsTransaction.user_id == current_user.id]
    if tx_type is not None:
        filters.append(_points_tx_type_filter(tx_type))
    total = (await db.execute(select(func.count(PointsTransaction.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(PointsTransaction).where(*filters).order_by(PointsTransaction.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {"total": total, "page": page, "per_page": per_page, "items": [PointsTransactionOut.model_validate(t) for t in rows]}


@router.get("/api/points/transactions/export")
async def export_my_points_transactions(
    tx_type: Optional[str] = Query(default=None, alias="type", description="earned | spent | refund | gifted"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-service CSV export of the caller's OWN points ledger — same CSV column shape
    as the moderator/admin-gated GET /api/admin/export/points-transactions
    (admin_misc.py), but scoped strictly to current_user.id with no user_id parameter
    accepted at all, matching the "only ever your own data" pattern GET /api/profile/
    export (routers/profile.py) already establishes for the GDPR JSON export — there is
    no way to make this act on anyone else's history. Optional `type` filter mirrors GET
    /api/points/transactions/me's, so the Points tab's active filter carries over into
    the download."""
    filters = [PointsTransaction.user_id == current_user.id]
    if tx_type is not None:
        filters.append(_points_tx_type_filter(tx_type))
    rows = (await db.execute(
        select(PointsTransaction).where(*filters).order_by(PointsTransaction.created_at.desc())
    )).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "delta", "balance_after", "reason", "detail"])
    for t in rows:
        w.writerow([t.created_at, t.delta, t.balance_after, t.reason, t.detail])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=my_points_history.csv"},
    )


@router.get("/api/points/earn-rate/me")
async def my_points_earn_rate(
    days: int = Query(default=30, ge=1, le=90),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simple rolling average of NET points/day over the trailing `days` days — sum of
    every PointsTransaction.delta in the window (earning AND spending both included, so
    a heavy recent spender correctly sees a lower/negative rate, not an inflated
    gross-earn number) divided by the window length. Backs the Points tab's "~N дней"
    affordability estimate next to wishlisted items the user can't yet afford (see
    profile.html). Deliberately simple, per the feature's own scope — no smoothing, no
    separate earn-vs-spend split; the frontend labels the result as an estimate."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = (await db.execute(
        select(func.coalesce(func.sum(PointsTransaction.delta), 0))
        .where(PointsTransaction.user_id == current_user.id, PointsTransaction.created_at >= cutoff)
    )).scalar_one()
    total = int(total)
    return {"window_days": days, "net_delta": total, "avg_per_day": total / days}
