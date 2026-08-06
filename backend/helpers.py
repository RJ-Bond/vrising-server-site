"""Shared helpers used across many/most route handlers, factored out of main.py so
router modules (backend/routers/*.py) can import them without importing main.py itself
(which would be circular once main.py imports the routers). Pure relocation — no logic
changes; see the "Split backend/main.py into routers" plan for the rationale."""
import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo
from pathlib import Path

from typing import Optional
from fastapi import Depends, HTTPException, Request, Response
from PIL import Image
from pywebpush import webpush, WebPushException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from .database import get_db
from .models import User, AuditLog, ServerApiKey, Setting, PointsTransaction, Ban, PluginHeartbeat, ScheduledRestart, PushSubscription, News
from .auth import COOKIE_NAME

logger = logging.getLogger(__name__)

# /data is the production Docker volume (docker-compose.yml) — overridable via env var
# so importing this module doesn't require a real /data to exist (it doesn't, and can't
# be created without root, on a bare CI runner or a local dev machine); scripts/
# check_backend.sh and test_backend.sh, plus .github/workflows/ci.yml, point these at a
# writable temp dir instead. This bit CI silently: it ran on a bare Linux runner where
# `/data` truly doesn't exist and PermissionError'd on mkdir — every run failed at this
# import before even reaching pytest, so the release job downstream never once fired.
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Shared with main.py's own _auto_backup_task (Background tasks, not yet split out) —
# same reason UPLOAD_DIR lives here rather than in routers/admin_system.py.
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/data/backups"))

# Optional offsite copy destination for the nightly auto-backup (_auto_backup_task in
# backend/main.py). Unset (default) = offsite copy is skipped entirely; BACKUP_DIR alone
# still gets its daily local backup either way — this only adds a second copy outside the
# `db_data` Docker volume, so a lost/corrupted host disk doesn't take the only backup copy
# down with it. There's no real remote-storage account to test against in this sandboxed
# environment, so this is deliberately just "copy this file to a path on the local
# filesystem" rather than talking to any specific cloud API — that path is expected to
# already be a mounted destination (e.g. an rclone remote mounted via `rclone mount`, or a
# plain NFS/SMB share bind-mounted into the `web` container). See README.md's "Резервные
# копии" section for how to actually wire one up.
BACKUP_REMOTE_PATH = os.getenv("BACKUP_REMOTE_PATH", "").strip()


def copy_backup_offsite(src: Path) -> bool:
    """Best-effort copy of a just-created local backup file to BACKUP_REMOTE_PATH, if
    set. Returns whether a copy was attempted and succeeded (False for both "nothing to
    do, BACKUP_REMOTE_PATH unset" and "attempted but failed") — never raises, since an
    offsite copy failing (remote unmounted, disk full, permission denied) must never be
    allowed to take down the local backup that already succeeded, or the retention
    cleanup that runs after it in the caller.

    Prefers `rsync` when the binary happens to be present (resumable, preserves mtime,
    only transfers changed bytes on a retry of the same file) and falls back to a plain
    `shutil.copy2` otherwise — the Docker image doesn't install rsync by default, so
    shutil.copy2 is the realistic path today; rsync is picked up automatically if a
    maintainer adds it to their own image/Dockerfile.
    """
    if not BACKUP_REMOTE_PATH:
        return False
    try:
        dest_dir = Path(BACKUP_REMOTE_PATH)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if shutil.which("rsync"):
            subprocess.run(["rsync", "-a", "--", str(src), str(dest)], check=True, timeout=300)
        else:
            shutil.copy2(str(src), str(dest))
        logger.info("Offsite backup copy: %s -> %s", src.name, dest)
        return True
    except Exception as e:
        logger.error("Offsite backup copy failed for %s: %s", src.name, e)
        return False

# Longest side, in px, that an uploaded photo is downscaled to. Site backgrounds/hero
# logos/avatars/covers are never displayed anywhere near source-camera resolution
# (a phone photo can be 4000px+ wide) — the mobile background-photo bug found this
# session was a 970x546 *already-small* image; a full-res upload would be much worse
# on mobile data. 2000px keeps desktop full-bleed banners sharp with real headroom.
_IMAGE_MAX_DIMENSION = 2000
_IMAGE_OPTIMIZABLE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def optimize_image_bytes(content: bytes, suffix: str) -> bytes:
    """Downscale + re-compress an uploaded raster image before it hits disk.

    Deliberately a no-op for .gif (re-saving via Pillow would collapse an animation to
    its first frame) and .ico (already tiny, and Pillow's ICO writer doesn't preserve
    multi-resolution icon sets). The source format is always preserved (PNG stays PNG,
    etc) rather than normalized to JPEG — several of these uploads are logos/icons that
    rely on transparency (site logo, hero logo, favicon), which a blind JPEG conversion
    would silently flatten onto an opaque background. Any decode/encode failure falls
    back to the original bytes rather than raising — a file that already passed the
    extension/MIME check should still upload even if it's some format quirk Pillow
    chokes on; this is a size optimization, not a validation gate.
    """
    if suffix.lower() not in _IMAGE_OPTIMIZABLE_SUFFIXES:
        return content
    try:
        img = Image.open(BytesIO(content))
        img.load()
        fmt = img.format
        width, height = img.size
        if max(width, height) > _IMAGE_MAX_DIMENSION:
            scale = _IMAGE_MAX_DIMENSION / max(width, height)
            img = img.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
        out = BytesIO()
        save_kwargs = {"optimize": True}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = 85
        img.save(out, format=fmt, **save_kwargs)
        optimized = out.getvalue()
    except Exception:
        return content
    # Re-encoding a tiny/already-optimal source (e.g. a small PNG icon) can come out
    # slightly larger than the original — only use the result if it actually helped.
    return optimized if len(optimized) < len(content) else content

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


async def _get_nickname_change_config(db: AsyncSession) -> dict:
    """Reads the ".nick" command's cost/cooldown Settings (admin.html's "Смена ника за
    очки" economy card). Cooldown has no dedicated column — it's derived from the most
    recent PointsTransaction row with reason="nickname_change" for that user instead, see
    POST /api/plugin/nickname-change."""
    res = await db.execute(select(Setting).where(Setting.key.in_(
        ["nickname_change_cost", "nickname_change_cooldown_days"]
    )))
    vals = {s.key: s.value for s in res.scalars().all()}

    def _to_int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    return {
        "cost": _to_int(vals.get("nickname_change_cost"), 100),
        "cooldown_days": _to_int(vals.get("nickname_change_cooldown_days"), 7),
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


# ─── Newsletter digest ─────────────────────────────────────────────────────────
# Weekly opt-in email covering news published since the last digest. Two callers
# share this: _newsletter_digest_task in main.py (the weekly schedule) and
# POST /api/admin/newsletter/send-now in admin_misc.py (an admin "send it right
# now" action) — both just call send_newsletter_digest(db), no separate code path
# for "manual" vs "scheduled".

NEWSLETTER_LAST_SENT_SETTING_KEY = "newsletter_last_sent_at"

_HTML_ESCAPE_CHARS = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def _html_escape(text: str) -> str:
    for src, dst in _HTML_ESCAPE_CHARS:
        text = text.replace(src, dst)
    return text


async def _newsletter_base_url(db: AsyncSession) -> str:
    """Same https_domain Setting + fallback host GET /api/rss.xml already uses for
    absolute news links, reused here so digest links resolve the same way regardless
    of whether the send was triggered by the background task (no Request object at
    all) or the admin manual-trigger endpoint."""
    base_url = "https://v.just-skill.ru"
    try:
        res = await db.execute(select(Setting).where(Setting.key == "https_domain"))
        s = res.scalar_one_or_none()
        if s and s.value.strip():
            base_url = f"https://{s.value.strip()}"
    except Exception:
        pass
    return base_url


async def send_newsletter_digest(db: AsyncSession) -> dict:
    """Email every opted-in, active user a digest of news published since the last
    digest send (Setting NEWSLETTER_LAST_SENT_SETTING_KEY) — title + short summary +
    link per item, plain HTML, no templating engine (see helpers.py's
    _send_notification_email, reused as-is rather than reinventing SMTP plumbing).
    First-ever run (no Setting row yet) looks back 7 days rather than dumping the
    entire news archive on whoever opts in before the first scheduled send.

    Always advances the cutoff Setting to "now" once it runs, even when there's
    nothing to send or nobody is opted in — a quiet week must not cause the next
    digest to double up on the same items. This also means calling this twice in a
    row (e.g. an admin manually triggering right after the weekly task already ran)
    is safe: the second call simply finds nothing new.

    Never raises: a per-recipient email failure is logged and skipped so one bad
    address can't stop the rest of the batch, matching the "best-effort, never
    breaks the caller" posture of send_push()/copy_backup_offsite() elsewhere in
    this file. Returns a small summary dict for the caller to log/return as JSON:
    {"items": n, "recipients": n, "sent": n}.
    """
    now = datetime.now(timezone.utc)
    res = await db.execute(select(Setting).where(Setting.key == NEWSLETTER_LAST_SENT_SETTING_KEY))
    setting = res.scalar_one_or_none()
    since = None
    if setting and setting.value:
        try:
            since = datetime.fromisoformat(setting.value)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            since = None
    if since is None:
        since = now - timedelta(days=7)

    # News.created_at is stored naive (SQLite) — compare against a naive UTC value,
    # same normalization approach as _utc_ts()/_fmt_dt() elsewhere in this file.
    since_naive = since.astimezone(timezone.utc).replace(tzinfo=None) if since.tzinfo else since
    news_res = await db.execute(
        select(News)
        .where(News.published == True, News.is_template == False, News.created_at > since_naive)
        .order_by(News.created_at.desc())
        .limit(20)
    )
    items = news_res.scalars().all()

    if setting:
        setting.value = now.isoformat()
    else:
        db.add(Setting(key=NEWSLETTER_LAST_SENT_SETTING_KEY, value=now.isoformat()))
    await db.commit()

    if not items:
        return {"items": 0, "recipients": 0, "sent": 0}

    users_res = await db.execute(
        select(User).where(User.newsletter_opt_in == True, User.is_active == True)
    )
    recipients = users_res.scalars().all()
    if not recipients:
        return {"items": len(items), "recipients": 0, "sent": 0}

    base_url = await _newsletter_base_url(db)
    subject = f"Новости V Rising — дайджест ({len(items)})"

    text_parts = ["Свежие новости на сайте:", ""]
    html_parts = ["<p>Свежие новости на сайте:</p>"]
    for n in items:
        link = f"{base_url}/?news={n.slug}"
        summary = (n.summary or "").strip()
        text_parts.append(f"{n.title}\n{summary}\n{link}\n")
        html_parts.append(
            '<div style="margin-bottom:16px;">'
            f'<a href="{link}" style="font-size:16px;font-weight:bold;text-decoration:none;">{_html_escape(n.title)}</a>'
            f'<p style="margin:4px 0;color:#444;">{_html_escape(summary)}</p>'
            "</div>"
        )
    text_parts.append("Чтобы отписаться от рассылки, отключите её в настройках профиля на сайте.")
    html_parts.append('<p style="color:#888;font-size:12px;">Чтобы отписаться от рассылки, отключите её в настройках профиля на сайте.</p>')
    text = "\n".join(text_parts)
    html = "".join(html_parts)

    sent = 0
    for user in recipients:
        try:
            ok = await _send_notification_email(user.email, subject, text, html)
            if ok:
                sent += 1
        except Exception:
            logger.warning("send_newsletter_digest: failed to send to user %s", user.id, exc_info=True)

    return {"items": len(items), "recipients": len(recipients), "sent": sent}


# ─── Web Push ─────────────────────────────────────────────────────────────────
# VAPID keypair identifies this server to push services (browser vendors), not any one
# user — same keypair for every subscription. Generate with `python scripts/generate_vapid_keys.py`
# (prints an env-ready private/public pair), store the private half server-side only
# (VAPID_PRIVATE_KEY), and expose the public half to the frontend via
# GET /api/push/vapid-public-key (see backend/routers/notifications.py) for
# `pushManager.subscribe({applicationServerKey: ...})`. Unset VAPID_PRIVATE_KEY = push
# silently disabled (send_push() no-ops), same "off by default, no crash" posture as
# SENTRY_DSN.
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
# "sub" claim of the VAPID JWT — must be a mailto: or https: URL identifying the
# sender, per RFC 8292. Push services (e.g. Mozilla's) may use it to contact the
# operator about a misbehaving sender; it is not shown to end users.
VAPID_CLAIMS_SUB = os.getenv("VAPID_CLAIMS_SUB", "mailto:admin@example.com")


async def send_push(user_id: int, title: str, body: str, url: str = "/") -> None:
    """Best-effort Web Push fan-out to every device the user has subscribed on (see
    PushSubscription in models.py) — always ALONGSIDE the in-app `Notification` row
    callers already `db.add()`, never a replacement for it. Must never raise: a
    misconfigured/unset VAPID key, a network hiccup, or an expired subscription would
    otherwise turn a like/reply/points-grant into a 500 for the *acting* user, who has
    nothing to do with the *recipient's* push setup. Expired/unregistered subscriptions
    (push service replies 404/410) are pruned so a dead endpoint doesn't get retried
    forever; anything else is logged and swallowed.

    Deliberately opens its own DB session (via database.AsyncSessionLocal, looked up on
    the module at call time so tests that monkeypatch it — see conftest.py's db_engine
    fixture — still take effect) rather than accepting the caller's `db` as a parameter.
    Every call site fires this via `asyncio.create_task(send_push(...))` so a slow/
    unreachable push service can't add latency to the user-facing request; reusing the
    request's own AsyncSession from a detached task would race its teardown (FastAPI
    closes it right after the endpoint returns, which can happen before the task gets a
    turn on the event loop).
    """
    if not VAPID_PRIVATE_KEY:
        return  # push not configured on this deployment
    from . import database

    try:
        async with database.AsyncSessionLocal() as db:
            res = await db.execute(select(PushSubscription).where(PushSubscription.user_id == user_id))
            subs = res.scalars().all()
            if not subs:
                return

            payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
            stale_ids = []
            for sub in subs:
                try:
                    # pywebpush's webpush() is a blocking (requests-based) call — run it
                    # off the event loop thread so one slow/unreachable push service
                    # can't stall every other coroutine on this worker.
                    await asyncio.to_thread(
                        webpush,
                        subscription_info={
                            "endpoint": sub.endpoint,
                            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                        },
                        data=payload,
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": VAPID_CLAIMS_SUB},
                        ttl=86400,
                        timeout=10,
                    )
                except WebPushException as e:
                    status_code = getattr(e.response, "status_code", None)
                    if status_code in (404, 410):
                        stale_ids.append(sub.id)
                    else:
                        logger.warning("send_push: webpush failed for user %s (status %s): %s", user_id, status_code, e)
                except Exception:
                    logger.warning("send_push: unexpected error pushing to user %s", user_id, exc_info=True)

            if stale_ids:
                await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(stale_ids)))
                await db.commit()
    except Exception:
        # Catches session-open/query/commit failures above — same "never break the
        # caller" contract as the per-subscription try/except inside the loop.
        logger.warning("send_push: unexpected error sending to user %s", user_id, exc_info=True)


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


# ─── Maintenance flag file ───────────────────────────────────────────────────
# Written/removed for nginx to read directly (no DB round-trip on every request). Lives
# here rather than in routers/admin_settings.py (its main caller) because main.py's own
# startup lifespan also restores it from the "maintenance_mode" Setting on boot — same
# sharing reason as _schedule_restart/_cancel_restart above.

MAINTENANCE_FLAG_PATH = "/var/maintenance/.flag"


def _write_maintenance_flag(enabled: bool) -> None:
    """Write or remove the maintenance flag file for nginx."""
    import pathlib
    try:
        flag = pathlib.Path(MAINTENANCE_FLAG_PATH)
        if enabled:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
    except Exception:
        pass


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


async def _get_linked_usernames(db: AsyncSession, steam_ids) -> dict:
    """steam_id -> username, for whichever of the given steam_ids belong to a registered,
    active site account (set via the plugin's .register/.login, see User.steam_id). Used to
    let bans.html link a banned character name to its site profile, if one exists."""
    ids = {s for s in steam_ids if s}
    if not ids:
        return {}
    result = await db.execute(
        select(User.steam_id, User.username).where(User.steam_id.in_(ids), User.is_active == True)
    )
    return {steam_id: username for steam_id, username in result.all()}
