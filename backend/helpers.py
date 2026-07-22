"""Shared helpers used across many/most route handlers, factored out of main.py so
router modules (backend/routers/*.py) can import them without importing main.py itself
(which would be circular once main.py imports the routers). Pure relocation — no logic
changes; see the "Split backend/main.py into routers" plan for the rationale."""
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from typing import Optional
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .database import get_db
from .models import User, AuditLog, ServerApiKey, Setting, PointsTransaction, Ban, PluginHeartbeat, ScheduledRestart
from .auth import COOKIE_NAME

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_totp_pending: dict[int, str] = {}

# Visitor-tracking state — logically part of the Who's-online domain (main.py, not yet
# split out) but also written by POST /api/auth/logout (backend/routers/auth.py) to
# retire a session immediately instead of waiting for its visitor-heartbeat entry to
# expire. Lives here (like _get_server_names/_force_unban below) purely so both sides
# can share the same dict without a main.py <-> routers circular import.
_visitor_data: dict[str, dict] = {}  # visitor_id -> {ts, first_ts, db_ts, page, username, is_authed, is_bot}
_explicit_logouts: dict[str, float] = {}  # username -> logout timestamp


def _fmt_dt(dt: datetime | None) -> str | None:
    """Return ISO-8601 string with explicit UTC offset so JS always parses as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _fmt_dt_z(dt: datetime | None) -> str | None:
    """Like _fmt_dt, but with a trailing "Z" instead of a "+00:00" offset — used for the
    scheduled-restart endpoints specifically so the C# plugin can unambiguously
    DateTime.Parse the value as UTC without needing DateTimeOffset handling."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _utc_ts(dt: datetime) -> float:
    """Unix epoch for a DB datetime. SQLite drops tzinfo, so a naive value must be
    treated as UTC — otherwise .timestamp() assumes the server's local zone and
    shifts the epoch (e.g. -3h on a Europe/Moscow host)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ─── Cookie helpers ──────────────────────────────────────────────────────────

_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # True only with HTTPS; nginx handles TLS termination
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


async def log_audit(db: AsyncSession, admin: User, action: str, detail: str = "") -> None:
    db.add(AuditLog(admin_username=admin.username, action=action, detail=detail[:512]))


async def _audit(db: AsyncSession, admin_id: int, action: str, target_type: str = None, target_id: int = None, detail: str = None) -> None:
    """Structured audit log entry with target_type/target_id support."""
    res = await db.execute(select(User).where(User.id == admin_id))
    u = res.scalar_one_or_none()
    db.add(AuditLog(
        admin_username=u.username if u else str(admin_id),
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=(detail or "")[:500],
    ))


async def _award_points(db: AsyncSession, user: User, delta: int, reason: str, detail: str = None) -> None:
    """Adjusts a user's balance and appends the matching ledger row in one step. Callers
    still need to `await db.commit()` themselves afterward (this repo's usual pattern —
    see the Announcements CRUD endpoints). NOT safe for the redeem/spend path under
    concurrency: this reads/writes `user.points_balance` in Python, so two overlapping
    calls for the same user can race. Only used for earn/grant/refund paths (playtime,
    streak, admin grant, redemption cancel-refund) where a single admin/plugin caller is
    the only writer at a time; the actual spend path (POST /api/shop/redeem) uses a
    separate atomic conditional UPDATE instead — see that endpoint."""
    user.points_balance += delta
    db.add(PointsTransaction(
        user_id=user.id, delta=delta, balance_after=user.points_balance,
        reason=reason, detail=(detail or "")[:256], created_at=datetime.utcnow(),
    ))


async def _get_points_config(db: AsyncSession) -> dict:
    """Reads the three points-economy earning-rate Settings, parsed to int with a sane
    fallback if a row is somehow missing (e.g. a DB that predates this feature and hasn't
    gone through _seed_defaults yet)."""
    res = await db.execute(select(Setting).where(Setting.key.in_(
        ["points_per_minute_playtime", "points_streak_bonus", "points_streak_min_days"]
    )))
    vals = {s.key: s.value for s in res.scalars().all()}

    def _to_int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    return {
        "per_minute": _to_int(vals.get("points_per_minute_playtime"), 1),
        "streak_bonus": _to_int(vals.get("points_streak_bonus"), 10),
        "streak_min_days": _to_int(vals.get("points_streak_min_days"), 2),
    }


async def _send_reset_email(to_email: str, reset_url: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        logger.info("SMTP not configured, skipping email to %s. Reset URL: %s", to_email, reset_url)
        return False
    try:
        import aiosmtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        password = os.getenv("SMTP_PASS", "")
        from_addr = os.getenv("SMTP_FROM", "noreply@localhost")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Сброс пароля"
        msg["From"] = from_addr
        msg["To"] = to_email
        text = f"Для сброса пароля перейдите по ссылке:\n\n{reset_url}\n\nСсылка действительна 24 часа."
        html = f"""<p>Для сброса пароля перейдите по ссылке:</p>
<p><a href="{reset_url}">{reset_url}</a></p>
<p>Ссылка действительна 24 часа.</p>"""
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        await aiosmtplib.send(
            msg, hostname=host, port=port,
            username=user or None, password=password or None,
            start_tls=(port == 587),
        )
        return True
    except Exception as e:
        logger.error("Failed to send reset email: %s", e)
        return False


async def _send_notification_email(to_email: str, subject: str, body_text: str, body_html: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return False
    try:
        import aiosmtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        password = os.getenv("SMTP_PASS", "")
        from_addr = os.getenv("SMTP_FROM", "noreply@localhost")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        await aiosmtplib.send(
            msg, hostname=host, port=port,
            username=user or None, password=password or None,
            start_tls=(port == 587),
        )
        return True
    except Exception as e:
        logger.error("Failed to send notification email: %s", e)
        return False


# ─── Game Plugin Integration ──────────────────────────────────────────────────
# Server-to-site channel used by the BepInEx plugin (vrising-bepinex-plugin) for the
# in-game .register/.login chat commands. Authenticated via a shared secret sent as the
# X-Plugin-Key header — this is the game server itself calling as a trusted caller, not
# an individual player, so it does not go through the user JWT scheme at all.
#
# Each server_num may optionally have its own key (ServerApiKey table, managed via
# GET/PUT /api/admin/server-api-key below) for better isolation — a leaked config only
# compromises the one server. Servers without a row there fall back to the single
# global secret (Setting "plugin_api_key", set in admin settings), preserving backward
# compatibility with already-deployed plugin configs that only know the shared key.

async def _require_plugin_key(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    provided = request.headers.get("X-Plugin-Key", "")
    if not provided:
        raise HTTPException(status_code=401, detail="Invalid or missing plugin key")

    # Determine which server this call is for: query param first (GET endpoints like
    # /api/plugin/announcements?server_num=N), else the JSON body (POST endpoints like
    # /api/plugin/heartbeat send server_num as a body field). Starlette caches the raw
    # request body internally, so reading it here via request.json() does not prevent
    # the endpoint's own Pydantic model from reading it again afterward.
    server_num: Optional[int] = None
    raw = request.query_params.get("server_num")
    if raw is not None:
        try:
            server_num = int(raw)
        except ValueError:
            server_num = None
    if server_num is None:
        try:
            body = await request.json()
            if isinstance(body, dict) and "server_num" in body:
                server_num = int(body["server_num"])
        except Exception:
            server_num = None
    if server_num is None:
        server_num = 1

    per_server_result = await db.execute(select(ServerApiKey).where(ServerApiKey.server_num == server_num))
    per_server = per_server_result.scalar_one_or_none()
    if per_server is not None:
        # A per-server key is configured — it alone is valid for this server_num, no
        # fallback to the global key (opting into an isolated key should mean isolated).
        if provided != per_server.api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing plugin key")
        return

    result = await db.execute(select(Setting).where(Setting.key == "plugin_api_key"))
    setting = result.scalar_one_or_none()
    expected = (setting.value if setting else "") or ""
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing plugin key")


async def _site_timezone(db: AsyncSession) -> ZoneInfo:
    """Site-configured display timezone (Setting "timezone", default Europe/Moscow) —
    same lookup/fallback pattern as the hourly-heatmap timezone lookup elsewhere in this
    file. Used to interpret wall-clock values (wipe_date/wipe_date2 Settings,
    ScheduledRestart.daily_restart_time) that are stored/entered in local site time."""
    tz_res = await db.execute(select(Setting).where(Setting.key == "timezone"))
    tz_setting = tz_res.scalar_one_or_none()
    tz_name = tz_setting.value if tz_setting else None
    try:
        return ZoneInfo(tz_name or "Europe/Moscow")
    except Exception:
        return ZoneInfo("Europe/Moscow")


# ─── Scheduled server restart ────────────────────────────────────────────────
# Shared by the plugin-facing restart endpoints (backend/routers/plugin_integration.py)
# and their admin-panel counterpart (backend/routers/server_admin.py) — both act on the
# same ScheduledRestart row and must stay in sync, so the helpers live here rather than
# in either router.

async def _schedule_restart(db: AsyncSession, server_num: int, minutes: int) -> datetime:
    if minutes < 1:
        raise HTTPException(status_code=400, detail="invalid_minutes")
    restart_at = datetime.utcnow() + timedelta(minutes=minutes)
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    if row is None:
        row = ScheduledRestart(server_num=server_num)
        db.add(row)
    row.restart_at = restart_at
    await db.commit()
    return restart_at


async def _cancel_restart(db: AsyncSession, server_num: int) -> None:
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    if row is not None and row.restart_at is not None:
        row.restart_at = None
        await db.commit()


def _force_unban(ban: Ban) -> None:
    """Brings unban_at forward to "now" (regardless of its previous value) rather than
    setting unbanned_at directly — that only happens once the plugin actually confirms the
    real in-game unban via POST /api/plugin/unban or the next GET /api/plugin/due-unbans
    poll consumes this row (within ~60s). Shared by POST /api/admin/bans/{id}/unban and
    the ban-appeal auto-lift in POST /api/admin/appeals/{id}/resolve (approve=true), so
    both "Разбанить" paths behave identically."""
    ban.unban_at = datetime.utcnow()


# ─── Clans (game-synced, read-only) ───────────────────────────────────────────
# Clan data is owned by the game itself — the plugin pushes the full current roster to
# POST /api/plugin/clans/sync (see "Game Plugin Integration" above). The website only
# ever displays it; there is no web-managed create/join/leave/delete anymore.

async def _get_server_names(db: AsyncSession) -> dict:
    """server_num -> real server name, sourced from the plugin's own heartbeat (the actual
    ServerHostSettings.json "Name", not a site setting that could drift out of sync)."""
    result = await db.execute(select(PluginHeartbeat.server_num, PluginHeartbeat.server_name))
    return {num: name for num, name in result.all() if name}
