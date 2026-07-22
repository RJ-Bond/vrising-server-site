import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import User
from ..auth import get_current_user, is_at_least
from ..rate_limit import limiter
from ..helpers import UPLOAD_DIR, _fmt_dt, _explicit_logouts

router = APIRouter()


# ─── Profile cover ───────────────────────────────────────────────────────────

@router.post("/api/profile/cover")
@limiter.limit("10/minute")
async def upload_cover(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Only image files are allowed")
    ext = Path(file.filename).suffix.lower() if file.filename else ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")
    covers_dir = UPLOAD_DIR / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    fname = f"cover_{current_user.id}_{uuid.uuid4().hex[:10]}{ext}"
    (covers_dir / fname).write_bytes(content)
    # Remove old cover file
    old = current_user.cover_url or ""
    if old:
        old_name = old.rsplit("/", 1)[-1]
        old_path = covers_dir / old_name
        if old_name.startswith("cover_") and old_path.exists():
            old_path.unlink(missing_ok=True)
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.cover_url = f"/api/uploads/covers/{fname}"
    await db.commit()
    return {"cover_url": user.cover_url}


# ─── Profile bio ─────────────────────────────────────────────────────────────

class BioBody(BaseModel):
    bio: Optional[str] = None

@router.put("/api/profile/bio")
@limiter.limit("20/minute")
async def update_bio(
    request: Request,
    body: BioBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bio = (body.bio or "").strip()[:160] or None
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.bio = bio
    await db.commit()
    return {"bio": user.bio}


# ─── Game nickname ────────────────────────────────────────────────────────────

class GameNicknameBody(BaseModel):
    game_nickname: str


@router.put("/api/profile/game-nickname")
async def update_game_nickname(
    body: GameNicknameBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    nick = body.game_nickname.strip()[:64]
    current_user.game_nickname = nick or None
    await db.commit()
    return {"game_nickname": current_user.game_nickname}


# ─── Team ────────────────────────────────────────────────────────────────────

@router.get("/api/team")
async def get_team(db: AsyncSession = Depends(get_db)):
    # Public staff roster — admin tier and up. Moderators stay internal/non-public,
    # consistent with the usual mod/admin distinction.
    result = await db.execute(
        select(User).where(User.role.in_(("admin", "superadmin")), User.is_active == True).order_by(User.created_at)
    )
    admins = result.scalars().all()
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff_5min = now_naive - timedelta(minutes=5)
    return [
        {
            "id": u.id,
            "username": u.username,
            "avatar_url": u.avatar_url,
            "created_at": _fmt_dt(u.created_at),
            "admin_title": u.admin_title,
            "last_active_at": _fmt_dt(u.last_active_at),
            "is_online": (
                u.username not in _explicit_logouts
                and u.last_active_at is not None
                and (u.last_active_at.replace(tzinfo=None) if u.last_active_at.tzinfo else u.last_active_at) >= cutoff_5min
            ),
            "badge_icon_url": u.badge_icon_url,
            "badge_style": u.badge_style or "default",
        }
        for u in admins
    ]


class AdminTitleBody(BaseModel):
    title: str


@router.put("/api/profile/admin-title")
async def set_admin_title(
    body: AdminTitleBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_at_least(current_user, "moderator"):
        raise HTTPException(status_code=403, detail="Только для администраторов")
    title = body.title.strip()[:128]
    current_user.admin_title = title or None
    await db.commit()
    return {"admin_title": current_user.admin_title}


@router.post("/api/profile/badge-icon")
@limiter.limit("10/minute")
async def upload_badge_icon(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_at_least(current_user, "moderator"):
        raise HTTPException(status_code=403, detail="Только для администраторов")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Только изображения")
    ext = Path(file.filename).suffix.lower() if file.filename else ".png"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:  # no .svg — see /api/admin/upload
        ext = ".png"
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Максимальный размер 2 МБ")
    fname = f"badge_{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    (UPLOAD_DIR / fname).write_bytes(content)
    old = (current_user.badge_icon_url or "").rsplit("/", 1)[-1]
    if old.startswith("badge_"):
        (UPLOAD_DIR / old).unlink(missing_ok=True)
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.badge_icon_url = f"/api/uploads/{fname}"
    await db.commit()
    return {"badge_icon_url": user.badge_icon_url}


@router.delete("/api/profile/badge-icon", status_code=204)
async def clear_badge_icon(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_at_least(current_user, "moderator"):
        raise HTTPException(status_code=403, detail="Только для администраторов")
    old = (current_user.badge_icon_url or "").rsplit("/", 1)[-1]
    if old.startswith("badge_"):
        (UPLOAD_DIR / old).unlink(missing_ok=True)
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.badge_icon_url = None
    await db.commit()


class BadgeStyleBody(BaseModel):
    style: str


_BADGE_STYLES = {"default", "crown", "shield", "diamond", "flame", "swords"}


@router.put("/api/profile/badge-style")
async def set_badge_style(
    body: BadgeStyleBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not is_at_least(current_user, "moderator"):
        raise HTTPException(status_code=403, detail="Только для администраторов")
    if body.style not in _BADGE_STYLES:
        raise HTTPException(status_code=400, detail="Недопустимый стиль")
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.badge_style = body.style
    await db.commit()
    return {"badge_style": user.badge_style}
