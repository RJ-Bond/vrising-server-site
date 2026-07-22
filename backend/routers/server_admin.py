import re

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import User, ServerMessageTemplate, ServerApiKey, ScheduledRestart
from ..auth import get_admin_user
from ..helpers import _audit, _fmt_dt_z, _schedule_restart, _cancel_restart
from ..schemas import (
    ServerMessageTemplateOut,
    ServerMessageTemplateUpdate,
    ServerApiKeyOut,
    ServerApiKeyUpdate,
)

router = APIRouter()


# ─── Per-server message templates (admin) ──────────────────────────────────────
# Connect/disconnect in-game chat message text, one row per server_num (ServerMessageTemplate
# model), replacing the old global "connect_message_template"/"disconnect_message_template"
# Settings now that the plugin runs on more than one server. Consumed by the plugin via
# GET /api/plugin/message-templates?server_num=N above.

@router.get("/api/admin/message-templates", response_model=ServerMessageTemplateOut)
async def get_message_templates(
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(
        select(ServerMessageTemplate).where(ServerMessageTemplate.server_num == server_num)
    )
    row = result.scalar_one_or_none()
    return ServerMessageTemplateOut(
        connect=(row.connect_template or "") if row else "",
        disconnect=(row.disconnect_template or "") if row else "",
    )


@router.put("/api/admin/message-templates", response_model=ServerMessageTemplateOut)
async def update_message_templates(
    body: ServerMessageTemplateUpdate,
    server_num: int = Query(default=1),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial update (exclude_unset), same convention as AnnouncementUpdate — a field
    omitted from the body leaves that side of the row untouched. Upserts the row for
    server_num on first save."""
    result = await db.execute(
        select(ServerMessageTemplate).where(ServerMessageTemplate.server_num == server_num)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = ServerMessageTemplate(server_num=server_num)
        db.add(row)
    updates = body.model_dump(exclude_unset=True)
    if "connect" in updates:
        row.connect_template = updates["connect"]
    if "disconnect" in updates:
        row.disconnect_template = updates["disconnect"]
    await _audit(db, current_user.id, "message_templates.update", target_type="server_message_template", target_id=server_num)
    await db.commit()
    return ServerMessageTemplateOut(
        connect=row.connect_template or "",
        disconnect=row.disconnect_template or "",
    )


# ─── Per-server plugin API key (admin) ─────────────────────────────────────────
# Optional per-server override of the global "plugin_api_key" Setting — see the
# _require_plugin_key docstring/comment above for the full precedence rules.

@router.get("/api/admin/server-api-key", response_model=ServerApiKeyOut)
async def get_server_api_key(
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(ServerApiKey).where(ServerApiKey.server_num == server_num))
    row = result.scalar_one_or_none()
    return ServerApiKeyOut(api_key=row.api_key if row else "")


@router.put("/api/admin/server-api-key", response_model=ServerApiKeyOut)
async def update_server_api_key(
    body: ServerApiKeyUpdate,
    server_num: int = Query(default=1),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """An empty api_key clears the override (deletes the row) so the server reverts to
    the global fallback key, rather than being stored as a literal empty-string secret."""
    result = await db.execute(select(ServerApiKey).where(ServerApiKey.server_num == server_num))
    row = result.scalar_one_or_none()
    value = body.api_key.strip()
    if not value:
        if row is not None:
            await db.delete(row)
        await _audit(db, current_user.id, "server_api_key.clear", target_type="server_api_key", target_id=server_num)
        await db.commit()
        return ServerApiKeyOut(api_key="")

    if row is None:
        row = ServerApiKey(server_num=server_num, api_key=value)
        db.add(row)
    else:
        row.api_key = value
    await _audit(db, current_user.id, "server_api_key.update", target_type="server_api_key", target_id=server_num)
    await db.commit()
    return ServerApiKeyOut(api_key=value)


# ─── Scheduled server restart (admin) ──────────────────────────────────────────
# Admin-panel counterpart to POST /api/plugin/schedule-restart / cancel-restart
# (backend/routers/plugin_integration.py) — shares the same ScheduledRestart row and
# _schedule_restart/_cancel_restart helpers (backend/helpers.py) so the site admin panel
# and an in-game admin chat command can't get out of sync.

@router.get("/api/admin/servers/{server_num}/restart")
async def get_scheduled_restart(
    server_num: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Same response shape as GET /api/plugin/restart-status, for the admin panel to show
    current countdown state on page load / server-tab switch: {"restart_at": iso | null}."""
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    return {"restart_at": _fmt_dt_z(row.restart_at if row else None)}


class AdminScheduleRestartBody(BaseModel):
    minutes: int


@router.post("/api/admin/servers/{server_num}/restart")
async def schedule_restart_admin(
    server_num: int,
    body: AdminScheduleRestartBody,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    restart_at = await _schedule_restart(db, server_num, body.minutes)
    await _audit(db, current_user.id, "restart.schedule", target_type="scheduled_restart", target_id=server_num, detail=f"{body.minutes}m")
    await db.commit()
    return {"restart_at": _fmt_dt_z(restart_at)}


@router.delete("/api/admin/servers/{server_num}/restart")
async def cancel_restart_admin(
    server_num: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    await _cancel_restart(db, server_num)
    await _audit(db, current_user.id, "restart.cancel", target_type="scheduled_restart", target_id=server_num)
    await db.commit()
    return {"success": True}


# ─── Recurring daily restart (admin) ───────────────────────────────────────────
# An independent recurring schedule layered on top of the one-off restart above — see
# ScheduledRestart.daily_restart_time's docstring in models.py and the self-arming logic
# in GET /api/plugin/restart-status. Managed only from the admin panel (no plugin-facing
# set/clear endpoint — an in-game admin command sets a one-off restart via the existing
# schedule-restart/cancel-restart pair, not the recurring schedule).

_DAILY_RESTART_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class AdminDailyRestartBody(BaseModel):
    time: str


def _validate_daily_restart_time(value: str) -> None:
    if not _DAILY_RESTART_TIME_RE.match(value):
        raise HTTPException(status_code=400, detail="invalid_time")
    hour, minute = (int(p) for p in value.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=400, detail="invalid_time")


@router.get("/api/admin/servers/{server_num}/daily-restart")
async def get_daily_restart(
    server_num: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    return {"daily_restart_time": row.daily_restart_time if row else None}


@router.post("/api/admin/servers/{server_num}/daily-restart")
async def set_daily_restart(
    server_num: int,
    body: AdminDailyRestartBody,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_daily_restart_time(body.time)
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    if row is None:
        row = ScheduledRestart(server_num=server_num)
        db.add(row)
    row.daily_restart_time = body.time
    await _audit(db, current_user.id, "daily_restart.set", target_type="scheduled_restart", target_id=server_num, detail=body.time)
    await db.commit()
    return {"daily_restart_time": body.time}


@router.delete("/api/admin/servers/{server_num}/daily-restart")
async def clear_daily_restart(
    server_num: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Independent of the one-off restart cleared by DELETE .../restart above — this only
    ever touches daily_restart_time, never restart_at."""
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    if row is not None and row.daily_restart_time is not None:
        row.daily_restart_time = None
        await _audit(db, current_user.id, "daily_restart.clear", target_type="scheduled_restart", target_id=server_num)
        await db.commit()
    return {"success": True}
