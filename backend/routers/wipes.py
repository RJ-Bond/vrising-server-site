from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import User, Wipe
from ..auth import get_admin_user
from ..schemas import WipeCreate, WipeOut

router = APIRouter()


# ─── Wipes ───────────────────────────────────────────────────────────────────

@router.get("/api/wipes", response_model=list[WipeOut])
async def get_wipes(response: Response, db: AsyncSession = Depends(get_db)):
    # Wipe history changes rarely (an admin action, not a live feed) — a short
    # shared cache takes load off repeat visitors without risking a stale-looking
    # page for long after a real wipe is recorded.
    response.headers["Cache-Control"] = "public, max-age=120"
    result = await db.execute(select(Wipe).order_by(Wipe.wipe_date.desc()))
    return [WipeOut.model_validate(w) for w in result.scalars().all()]


@router.post("/api/admin/wipes", response_model=WipeOut, status_code=201)
async def create_wipe(
    body: WipeCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    wipe = Wipe(**body.model_dump())
    db.add(wipe)
    await db.commit()
    await db.refresh(wipe)
    return WipeOut.model_validate(wipe)


@router.delete("/api/admin/wipes/{wipe_id}", status_code=204)
async def delete_wipe(
    wipe_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(Wipe).where(Wipe.id == wipe_id))
    wipe = result.scalar_one_or_none()
    if wipe is None:
        raise HTTPException(status_code=404, detail="Wipe not found")
    await db.delete(wipe)
    await db.commit()
