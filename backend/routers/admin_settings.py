import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import User, Setting
from ..auth import get_current_user, get_admin_user, is_at_least
from ..helpers import _audit, _write_maintenance_flag
from ..schemas import SettingOut, SettingUpdate

router = APIRouter()


# ─── Settings (public) ───────────────────────────────────────────────────────

async def _set_setting_value(db: AsyncSession, key: str, value: str) -> None:
    res = await db.execute(select(Setting).where(Setting.key == key))
    s = res.scalar_one_or_none()
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    await db.commit()


async def _send_maintenance_webhook(db: AsyncSession, enabled: bool) -> None:
    try:
        res = await db.execute(select(Setting).where(Setting.key.in_(["discord_webhook_url", "site_title"])))
        smap = {s.key: s.value for s in res.scalars()}
        url = smap.get("discord_webhook_url", "")
        if not url:
            return
        title = smap.get("site_title", "V Rising")
        color = 0xFF4444 if enabled else 0x44FF88
        msg = "🔧 Режим обслуживания **включён**" if enabled else "✅ Сайт снова **доступен**"
        payload = {
            "embeds": [{
                "title": msg,
                "color": color,
                "footer": {"text": title},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }]
        }
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(url, json=payload)
    except Exception:
        pass


async def _record_maintenance_history(db: AsyncSession, enabled: bool) -> None:
    """Record maintenance episode start/end in history."""
    try:
        res = await db.execute(select(Setting).where(Setting.key == "maintenance_history"))
        s = res.scalar_one_or_none()
        history = json.loads(s.value if s and s.value else "[]")
        now_iso = datetime.now(timezone.utc).isoformat()
        if enabled:
            # Start new episode
            history.insert(0, {"start": now_iso, "end": None, "duration": None})
        else:
            # Close last open episode
            for ep in history:
                if ep.get("end") is None:
                    ep["end"] = now_iso
                    start_dt = datetime.fromisoformat(ep["start"])
                    end_dt = datetime.fromisoformat(now_iso)
                    ep["duration"] = int((end_dt - start_dt).total_seconds())
                    break
        # Keep last 50 episodes
        history = history[:50]
        await _set_setting_value(db, "maintenance_history", json.dumps(history, ensure_ascii=False))
    except Exception:
        pass


@router.get("/api/settings/public")
async def get_public_settings(db: AsyncSession = Depends(get_db)):
    keys = ["site_title", "site_tagline", "site_description", "site_logo_url", "hero_logo_url", "hero_subtitle", "favicon_url", "discord_url", "discord_server_id", "max_url", "bg_image_url", "server_ip", "server_port", "server_name", "server2_name", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2", "event_active", "event_title", "event_text", "event_color", "rules", "timezone", "time_format", "date_format", "maintenance_mode", "maintenance_title", "maintenance_message", "maintenance_video_url", "maintenance_end_time", "maintenance_start_time", "maintenance_fallback_image", "maintenance_status_updates", "maintenance_history", "nav_hidden"]
    result = await db.execute(select(Setting).where(Setting.key.in_(keys)))
    settings = result.scalars().all()
    d = {s.key: s.value for s in settings}

    # Auto-enable / auto-disable maintenance by schedule
    now_iso = datetime.now(timezone.utc).isoformat()
    start_t = d.get("maintenance_start_time", "")
    end_t   = d.get("maintenance_end_time", "")
    mode    = d.get("maintenance_mode", "false")

    if start_t and mode == "false" and start_t <= now_iso:
        # Auto-enable
        await _set_setting_value(db, "maintenance_mode", "true")
        d["maintenance_mode"] = "true"
        asyncio.create_task(_send_maintenance_webhook(db, enabled=True))
        asyncio.create_task(_record_maintenance_history(db, True))
        _write_maintenance_flag(True)

    if end_t and mode == "true" and end_t <= now_iso:
        # Auto-disable
        await _set_setting_value(db, "maintenance_mode", "false")
        d["maintenance_mode"] = "false"
        asyncio.create_task(_send_maintenance_webhook(db, enabled=False))
        asyncio.create_task(_record_maintenance_history(db, False))
        _write_maintenance_flag(False)

    return d


# ─── Maintenance status updates ──────────────────────────────────────────────

class MaintenanceStatusBody(BaseModel):
    text: str

@router.post("/api/admin/maintenance/status", status_code=200)
async def add_maintenance_status(
    body: MaintenanceStatusBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_at_least(current_user, "admin"):
        raise HTTPException(403)
    body.text = body.text.strip()[:200]
    if not body.text:
        raise HTTPException(400, "Empty text")
    res = await db.execute(select(Setting).where(Setting.key == "maintenance_status_updates"))
    s = res.scalar_one_or_none()
    updates = json.loads(s.value if s and s.value else "[]")
    updates.insert(0, {"text": body.text, "ts": int(time.time())})
    updates = updates[:20]  # keep last 20
    await _set_setting_value(db, "maintenance_status_updates", json.dumps(updates, ensure_ascii=False))
    return {"ok": True, "updates": updates}


@router.delete("/api/admin/maintenance/status/{idx}", status_code=200)
async def delete_maintenance_status(
    idx: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_at_least(current_user, "admin"):
        raise HTTPException(403)
    res = await db.execute(select(Setting).where(Setting.key == "maintenance_status_updates"))
    s = res.scalar_one_or_none()
    updates = json.loads(s.value if s and s.value else "[]")
    if 0 <= idx < len(updates):
        updates.pop(idx)
    await _set_setting_value(db, "maintenance_status_updates", json.dumps(updates, ensure_ascii=False))
    return {"ok": True, "updates": updates}


class MaintenanceExtendBody(BaseModel):
    minutes: int  # 15, 30, 60, etc.

@router.post("/api/admin/maintenance/extend")
async def extend_maintenance(
    body: MaintenanceExtendBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_at_least(current_user, "admin"):
        raise HTTPException(403)
    if body.minutes not in (15, 30, 60, 120):
        raise HTTPException(400, "Invalid duration")
    # Get current end time
    res = await db.execute(select(Setting).where(Setting.key == "maintenance_end_time"))
    s = res.scalar_one_or_none()
    end_val = s.value if s and s.value else ""
    try:
        if end_val:
            base = datetime.fromisoformat(end_val)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
        else:
            base = datetime.now(timezone.utc)
        new_end = base + timedelta(minutes=body.minutes)
        new_end_iso = new_end.isoformat()
    except Exception:
        new_end_iso = (datetime.now(timezone.utc) + timedelta(minutes=body.minutes)).isoformat()
    await _set_setting_value(db, "maintenance_end_time", new_end_iso)
    return {"ok": True, "new_end": new_end_iso}


# ─── Settings (admin) ────────────────────────────────────────────────────────

ALLOWED_SETTING_KEYS = {
    "setup_completed", "server_ip", "server_port", "server_game_port", "server_connect_ip", "server_name",
    "server2_name", "server2_ip", "server2_port", "server2_game_port", "server2_connect_ip",
    "site_title", "site_tagline", "site_description", "site_logo_url", "hero_logo_url", "hero_subtitle", "favicon_url", "discord_url", "discord_server_id", "max_url",
    "bg_image_url", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2",
    "event_active", "event_title", "event_text", "event_color",
    "rules", "https_domain", "https_email",
    "timezone", "time_format", "date_format",
    "rcon_port", "rcon_password", "rcon2_port", "rcon2_password", "discord_webhook_url", "plugin_api_key", "server_announcement",
    "maintenance_mode", "maintenance_title", "maintenance_message", "maintenance_video_url", "maintenance_end_time",
    "maintenance_start_time", "maintenance_fallback_image", "maintenance_status_updates", "maintenance_history",
    "points_per_minute_playtime", "points_streak_bonus", "points_streak_min_days",
    "nav_hidden",
}

@router.get("/api/admin/settings", response_model=list[SettingOut])
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(Setting))
    return [SettingOut.model_validate(s) for s in result.scalars().all()]


@router.put("/api/admin/settings/{key}", response_model=SettingOut)
async def update_setting(
    key: str,
    body: SettingUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(get_admin_user),
):
    if key not in ALLOWED_SETTING_KEYS:
        raise HTTPException(400, f"Unknown setting key: {key}")
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = Setting(key=key, value=body.value)
        db.add(setting)
    else:
        setting.value = body.value
        setting.updated_at = datetime.now(timezone.utc)
    await _audit(db, _admin.id, "settings.update", detail=f"{key}={body.value[:100]}")
    await db.commit()
    await db.refresh(setting)
    if key == "maintenance_mode":
        asyncio.create_task(_send_maintenance_webhook(db, body.value == "true"))
        asyncio.create_task(_record_maintenance_history(db, body.value == "true"))
        _write_maintenance_flag(body.value == "true")
    return SettingOut.model_validate(setting)


# ─── Settings import ─────────────────────────────────────────────────────────

@router.post("/api/admin/settings/import")
async def import_settings(
    body: dict,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    count = 0
    for key, value in body.items():
        r = await db.execute(select(Setting).where(Setting.key == key))
        s = r.scalar_one_or_none()
        if s:
            s.value = str(value)
            s.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key=key, value=str(value)))
        count += 1
    await db.commit()
    return {"imported": count}
