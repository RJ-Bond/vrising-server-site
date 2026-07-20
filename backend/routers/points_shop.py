from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, update

from ..database import get_db
from ..models import User, PointsTransaction, ShopItem, ShopRedemption
from ..auth import get_admin_user, get_current_user
from ..helpers import _audit, _award_points
from ..schemas import (
    ShopItemCreate,
    ShopItemUpdate,
    ShopItemOut,
    ShopRedeemIn,
    ShopRedemptionResolveIn,
    ShopRedemptionOut,
    PointsGrantIn,
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
        sort_order=body.sort_order,
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
    """shop_item_id is ON DELETE SET NULL on ShopRedemption — past redemption history
    (item_name_snapshot/cost_snapshot) survives a catalog item being removed."""
    item = (await db.execute(select(ShopItem).where(ShopItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")
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
    return {"total": total, "page": page, "per_page": per_page, "items": items}


@router.post("/api/admin/shop/redemptions/{redemption_id}/fulfill", response_model=ShopRedemptionOut)
async def fulfill_shop_redemption(
    redemption_id: int,
    body: ShopRedemptionResolveIn = ShopRedemptionResolveIn(),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    r = (await db.execute(select(ShopRedemption).where(ShopRedemption.id == redemption_id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Redemption not found")
    if r.status != "pending":
        raise HTTPException(409, "Redemption is not pending")
    r.status = "fulfilled"
    r.resolved_at = datetime.utcnow()
    r.resolved_by = current_user.username
    if body.admin_note:
        r.admin_note = body.admin_note
    await _audit(db, current_user.id, "shop.redemption.fulfill", target_type="shop_redemption", target_id=r.id, detail=r.item_name_snapshot)
    await db.commit()
    await db.refresh(r)
    return ShopRedemptionOut.model_validate(r)


@router.post("/api/admin/shop/redemptions/{redemption_id}/cancel", response_model=ShopRedemptionOut)
async def cancel_shop_redemption(
    redemption_id: int,
    body: ShopRedemptionResolveIn = ShopRedemptionResolveIn(),
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
    r.resolved_at = datetime.utcnow()
    r.resolved_by = current_user.username
    if body.admin_note:
        r.admin_note = body.admin_note
    await _audit(db, current_user.id, "shop.redemption.cancel", target_type="shop_redemption", target_id=r.id, detail=r.item_name_snapshot)
    await db.commit()
    await db.refresh(r)
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
    await db.commit()
    tx_res = await db.execute(
        select(PointsTransaction).where(PointsTransaction.user_id == user.id).order_by(PointsTransaction.id.desc()).limit(1)
    )
    tx = tx_res.scalar_one()
    out = PointsTransactionOut.model_validate(tx)
    out.username = user.username
    return out


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


# ─── Points economy — shop (player-facing) ─────────────────────────────────────

@router.get("/api/shop/items", response_model=list[ShopItemOut])
async def list_shop_items_public(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Active items only. stock is included but NOT filtered out at stock=0 — the
    front-end greys those out instead of hiding them, so a player can still see what
    exists even when temporarily out of stock."""
    result = await db.execute(select(ShopItem).where(ShopItem.is_active == True).order_by(ShopItem.sort_order, ShopItem.id))
    return [ShopItemOut.model_validate(i) for i in result.scalars().all()]


@router.post("/api/shop/redeem", response_model=ShopRedemptionOut, status_code=201)
async def redeem_shop_item(
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
    stock. See backend/tests/test_points_shop.py's asyncio.gather concurrency test."""
    item_res = await db.execute(select(ShopItem).where(ShopItem.id == body.shop_item_id))
    item = item_res.scalar_one_or_none()
    if item is None or not item.is_active:
        raise HTTPException(404, "Item not found")

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


@router.get("/api/points/transactions/me")
async def my_points_transactions(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Caller's own full ledger — earn rows (playtime/streak) as well as spend/refund."""
    filters = [PointsTransaction.user_id == current_user.id]
    total = (await db.execute(select(func.count(PointsTransaction.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(PointsTransaction).where(*filters).order_by(PointsTransaction.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {"total": total, "page": page, "per_page": per_page, "items": [PointsTransactionOut.model_validate(t) for t in rows]}
