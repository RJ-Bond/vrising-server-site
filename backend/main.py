import csv
import html
import io
import logging
import os
import math
import re
import json
import uuid
import time
import asyncio
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from typing import Optional
from pydantic import BaseModel, field_validator
from fastapi import FastAPI, Depends, HTTPException, Request, Query, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Repo root is bind-mounted read/write at /opt/vrising-site (see docker-compose.yml) for
# the deploy/update endpoints; reused here to serve frontend/index.html for news-embed.
_INDEX_HTML_PATH = "/opt/vrising-site/frontend/index.html"
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, text, or_, update, and_
from sqlalchemy.orm import selectinload

from .database import engine, get_db
from .models import Base, User, News, Setting, Comment, Wipe, PlayerRecord, ServerSnapshot, AuditLog, Reaction, PasswordReset, CommentReaction, Notification, Report, Poll, PollOption, PollVote, PageView, ErrorLog, Message, RevokedToken, Event, EventParticipant, PlayerRankSnapshot, PluginHeartbeat, GameClan, GameClanMember, Announcement, ServerMessageTemplate, ServerApiKey, ScheduledRestart, Warning, Ban, BanAppeal, ModerationLogEntry, PlayerDailyActivity, PointsTransaction, ShopItem, ShopRedemption
from .rate_limit import limiter
from .helpers import (
    UPLOAD_DIR,
    _totp_pending,
    _fmt_dt,
    _fmt_dt_z,
    _utc_ts,
    _set_auth_cookie,
    _clear_auth_cookie,
    log_audit,
    _audit,
    _award_points,
    _get_points_config,
    _send_reset_email,
    _send_notification_email,
    _require_plugin_key,
    _site_timezone,
    _force_unban,
    _get_server_names,
)
from .auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    get_admin_user,
    get_moderator_user,
    get_superadmin_user,
    get_optional_user,
    revoke_token,
    role_level,
    is_at_least,
    ROLE_LEVELS,
    SECRET_KEY,
    ALGORITHM,
    COOKIE_NAME,
)
from jose import jwt as jose_jwt
from .schemas import (
    UserRegister,
    UserLogin,
    UserOut,
    TokenOut,
    NewsCreate,
    NewsUpdate,
    NewsOut,
    NewsListOut,
    PaginatedNews,
    SettingUpdate,
    SettingOut,
    SetupComplete,
    ChatRequest,
    CommentCreate,
    CommentUpdate,
    CommentOut,
    PaginatedComments,
    WipeCreate,
    WipeOut,
    PlayerRecordOut,
    ForgotPasswordRequest,
    ResetPasswordBody,
    ChangePasswordBody,
    ChangeEmailBody,
    ReactBody,
    ReportCreate,
    ReportReview,
    ReportOut,
    PollCreate,
    PollOut,
    PluginRegister,
    PluginLogin,
    PluginAcceptRules,
    PluginHeartbeatIn,
    PluginHeartbeatOut,
    PluginSessionReport,
    PluginConnectStreakIn,
    PluginClansSyncIn,
    GameClanOut,
    GameClanDetailOut,
    AnnouncementCreate,
    AnnouncementUpdate,
    AnnouncementOut,
    AnnouncementTestSend,
    ServerMessageTemplateOut,
    ServerMessageTemplateUpdate,
    ServerApiKeyOut,
    ServerApiKeyUpdate,
    PluginScheduleRestartIn,
    PluginCancelRestartIn,
    PluginWarnIn,
    PluginBanIn,
    PluginUnbanIn,
    BanAppealCreate,
    AppealResolveIn,
    PluginLogActionIn,
    LinkedAccountOut,
    ShopItemCreate,
    ShopItemUpdate,
    ShopItemOut,
    ShopRedeemIn,
    ShopRedemptionResolveIn,
    ShopRedemptionOut,
    PointsGrantIn,
    PointsTransactionOut,
    PointsLeaderboardEntryOut,
    strip_html_tags,
)

OVERSEER_PROMPT = """Ты — Тёмный Управляющий Замком, древний вампирский дух, хранитель этого сервера V Rising.
Твоя задача — помогать игрокам: отвечать на вопросы об игровом сервере, правилах, механиках V Rising, событиях.
Стиль: готический, величественный, слегка таинственный. Обращайся к игрокам как «смертный», «странник» или по имени.
Отвечай на языке вопроса (русский или английский). Максимум 3–4 предложения. Будь полезным и по делу.
Если не знаешь конкретных данных сервера — говори об этом честно, но оставайся в образе."""
from .monitor import get_server_status, get_history


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = re.sub(r"^-+|-+$", "", text)
    return text[:200]


async def _migrate_admin_role_tiers(db: AsyncSession):
    """One-time: promote every pre-existing role="admin" account to "superadmin".

    Before this migration, "admin" was the top tier — capable of backups/rcon/ssl/
    role-management. After it, those move to a new superadmin-only tier, so any
    existing admin account must be promoted or its owner silently loses capability
    they had a moment ago. There's no way to tell "the real owner" from "an admin
    added later" from the role string alone, so promoting everyone is the only safe
    default (under-promoting risks bricking someone's access; over-promoting doesn't
    remove anything anyone already had).

    Flag-gated so this runs exactly once — a second run must be a no-op, and must NOT
    touch a legitimately-created future "admin"-tier account.
    """
    flag_res = await db.execute(select(Setting).where(Setting.key == "role_tiers_migrated"))
    flag = flag_res.scalar_one_or_none()
    if flag and flag.value == "true":
        return
    await db.execute(update(User).where(User.role == "admin").values(role="superadmin"))
    if flag:
        flag.value = "true"
    else:
        db.add(Setting(key="role_tiers_migrated", value="true"))
    await db.commit()


async def _seed_defaults(db: AsyncSession):
    default_settings = [
        Setting(key="setup_completed", value="false"),
        Setting(key="server_ip", value=os.getenv("VRISING_SERVER_IP", "127.0.0.1")),
        Setting(key="server_port", value=os.getenv("VRISING_SERVER_PORT", "27016")),
        Setting(key="server_game_port", value=""),
        Setting(key="server_connect_ip", value=""),
        Setting(key="server_name", value="V Rising Server"),
        Setting(key="site_title", value="V RISING"),
        Setting(key="site_tagline", value="Замок"),
        Setting(key="site_description", value="Официальный сайт игрового сервера V Rising — новости, статус серверов, лидерборд, правила."),
        Setting(key="site_logo_url", value=""),
        Setting(key="discord_url", value=""),
        Setting(key="bg_image_url", value=""),
        Setting(key="server2_name", value=""),
        Setting(key="server2_ip", value=""),
        Setting(key="server2_port", value="27016"),
        Setting(key="server2_game_port", value=""),
        Setting(key="server2_connect_ip", value=""),
        Setting(key="discord_server_id", value=""),
        Setting(key="wipe_date", value=""),
        Setting(key="wipe_type", value="full"),
        Setting(key="wipe_date2", value=""),
        Setting(key="wipe_type2", value="full"),
        Setting(key="event_active", value="0"),
        Setting(key="event_title", value=""),
        Setting(key="event_text", value=""),
        Setting(key="event_color", value="crimson"),
        Setting(key="timezone", value="Europe/Moscow"),
        Setting(key="time_format", value="24h"),
        Setting(key="date_format", value="dd.mm.yyyy"),
        Setting(key="rules", value='[{"icon":"🤝","text":"Уважай других игроков — оскорбления и токсичное поведение запрещены"},{"icon":"🚫","text":"Читы, эксплойты и стороннее ПО — бан без предупреждения"},{"icon":"⚔","text":"Сервер PvE — атаки на других игроков запрещены"},{"icon":"🏰","text":"Запрещено разрушать, красть из построек или гриферить базы других игроков"},{"icon":"🪨","text":"Не перекрывай ресурсные точки и пути прохода своими строениями"},{"icon":"🌱","text":"Помогай новичкам — каждый когда-то начинал с нуля"},{"icon":"🔧","text":"Баги и нарушения сообщай администрации — не используй их в свою пользу"},{"icon":"💬","text":"Спорные ситуации решай через чат или обращайся к администратору"}]'),
        Setting(key="rcon_port", value="25575"),
        Setting(key="rcon_password", value=""),
        Setting(key="rcon2_port", value="25575"),
        Setting(key="rcon2_password", value=""),
        Setting(key="discord_webhook_url", value=""),
        Setting(key="plugin_api_key", value=""),
        Setting(key="server_announcement", value=""),
        Setting(key="maintenance_mode",    value="false"),
        Setting(key="maintenance_title",   value="Технические работы"),
        Setting(key="maintenance_message", value="Сайт временно недоступен. Скоро вернёмся."),
        Setting(key="maintenance_video_url", value=""),
        Setting(key="maintenance_end_time",  value=""),
        Setting(key="maintenance_start_time",    value=""),
        Setting(key="maintenance_fallback_image", value=""),
        Setting(key="maintenance_status_updates", value="[]"),
        Setting(key="maintenance_history", value="[]"),
        # Points economy — earning rates, tunable by an admin on the Economy tab
        # (not exposed on /api/settings/public: admin-only tuning, no anonymous use).
        Setting(key="points_per_minute_playtime", value="1"),
        Setting(key="points_streak_bonus", value="10"),
        Setting(key="points_streak_min_days", value="2"),
    ]
    for s in default_settings:
        existing = await db.execute(select(Setting).where(Setting.key == s.key))
        if existing.scalar_one_or_none() is None:
            db.add(s)
    await db.flush()

    # Если администратор уже существует — считаем настройку завершённой
    admin_result = await db.execute(select(User).where(User.role.in_(("admin", "superadmin"))).limit(1))
    if admin_result.scalar_one_or_none():
        sc = await db.execute(select(Setting).where(Setting.key == "setup_completed"))
        sc_row = sc.scalar_one_or_none()
        if sc_row and sc_row.value == "false":
            sc_row.value = "true"

    await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # add columns that may be missing in existing DBs (SQLite ALTER TABLE)
        for stmt in [
            "ALTER TABLE news ADD COLUMN tags VARCHAR(256) DEFAULT ''",
            "ALTER TABLE users ADD COLUMN avatar_url VARCHAR(512) DEFAULT NULL",
            "ALTER TABLE news ADD COLUMN views INTEGER DEFAULT 0 NOT NULL",
            "ALTER TABLE news ADD COLUMN pinned BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE users ADD COLUMN clan_id INTEGER DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN rules_accepted_at DATETIME DEFAULT NULL",
            "ALTER TABLE comments ADD COLUMN parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE",
            "CREATE TABLE IF NOT EXISTS comment_reactions (id INTEGER PRIMARY KEY, comment_id INTEGER REFERENCES comments(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, emoji VARCHAR(10) NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(comment_id, user_id, emoji))",
            "CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, type VARCHAR(32) NOT NULL, data TEXT NOT NULL DEFAULT '{}', read BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, read)",
            "ALTER TABLE users ADD COLUMN game_nickname VARCHAR(64) DEFAULT NULL",
            "ALTER TABLE news ADD COLUMN publish_at DATETIME DEFAULT NULL",
            "ALTER TABLE news ADD COLUMN is_template BOOLEAN DEFAULT 0 NOT NULL",
            "CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, reporter_id INTEGER REFERENCES users(id) ON DELETE SET NULL, target_type VARCHAR(32) NOT NULL, target_id INTEGER NOT NULL, reason VARCHAR(512) NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'pending', admin_note TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, reviewed_at DATETIME)",
            "CREATE TABLE IF NOT EXISTS polls (id INTEGER PRIMARY KEY, news_id INTEGER REFERENCES news(id) ON DELETE CASCADE, question VARCHAR(256) NOT NULL, multiple BOOLEAN NOT NULL DEFAULT 0, ends_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS poll_options (id INTEGER PRIMARY KEY, poll_id INTEGER REFERENCES polls(id) ON DELETE CASCADE, text VARCHAR(256) NOT NULL)",
            "CREATE TABLE IF NOT EXISTS poll_votes (id INTEGER PRIMARY KEY, poll_id INTEGER REFERENCES polls(id) ON DELETE CASCADE, option_id INTEGER REFERENCES poll_options(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(poll_id, user_id))",
            "CREATE TABLE IF NOT EXISTS page_views (id INTEGER PRIMARY KEY, path VARCHAR(256) NOT NULL, ip_hash VARCHAR(64), created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS ix_page_views_date ON page_views(created_at)",
            "CREATE TABLE IF NOT EXISTS error_logs (id INTEGER PRIMARY KEY, path VARCHAR(256) NOT NULL, method VARCHAR(8) NOT NULL DEFAULT 'GET', status_code INTEGER NOT NULL, error TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS ix_error_logs_date ON error_logs(created_at)",
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, sender_id INTEGER REFERENCES users(id) ON DELETE CASCADE, recipient_id INTEGER REFERENCES users(id) ON DELETE CASCADE, content TEXT NOT NULL, read BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS ix_messages_recipient ON messages(recipient_id, read)",
            "CREATE INDEX IF NOT EXISTS ix_messages_sender ON messages(sender_id)",
            "CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(sender_id, recipient_id)",
            "ALTER TABLE users ADD COLUMN admin_title VARCHAR(128) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN last_active_at DATETIME DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN badge_icon_url VARCHAR(512) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN badge_style VARCHAR(32) DEFAULT 'default'",
            "CREATE TABLE IF NOT EXISTS revoked_tokens (id INTEGER PRIMARY KEY, token VARCHAR(512) NOT NULL UNIQUE, expires_at DATETIME NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS ix_revoked_tokens_token ON revoked_tokens(token)",
            "ALTER TABLE users ADD COLUMN revoke_before DATETIME DEFAULT NULL",
            "ALTER TABLE player_records ADD COLUMN session_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN cover_url VARCHAR(512) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN totp_secret VARCHAR(64) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE audit_log ADD COLUMN target_type VARCHAR(50) DEFAULT NULL",
            "ALTER TABLE audit_log ADD COLUMN target_id INTEGER DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN bio VARCHAR(160) DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN steam_id VARCHAR(32) DEFAULT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_steam_id ON users(steam_id)",
            "CREATE TABLE IF NOT EXISTS plugin_heartbeats (server_num INTEGER PRIMARY KEY, server_name VARCHAR(128), plugin_version VARCHAR(32), player_count INTEGER NOT NULL DEFAULT 0, last_seen_at DATETIME NOT NULL)",
            "CREATE TABLE IF NOT EXISTS game_clans (id INTEGER PRIMARY KEY, server_num INTEGER NOT NULL DEFAULT 1, clan_guid VARCHAR(36) NOT NULL, name VARCHAR(64) NOT NULL, motto VARCHAR(64) DEFAULT '', updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(server_num, clan_guid))",
            "CREATE TABLE IF NOT EXISTS game_clan_members (id INTEGER PRIMARY KEY, clan_id INTEGER NOT NULL REFERENCES game_clans(id) ON DELETE CASCADE, steam_id VARCHAR(32) NOT NULL, character_name VARCHAR(64) NOT NULL, role VARCHAR(16) NOT NULL DEFAULT 'member')",
            "CREATE INDEX IF NOT EXISTS ix_game_clan_members_clan ON game_clan_members(clan_id)",
            "CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY, text TEXT NOT NULL, interval_minutes INTEGER, enabled BOOLEAN NOT NULL DEFAULT 1, expires_at DATETIME, last_sent_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "ALTER TABLE announcements ADD COLUMN target_steam_id VARCHAR(32) DEFAULT NULL",
            "ALTER TABLE player_records ADD COLUMN steam_id VARCHAR(32) DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS ix_player_records_steam_id ON player_records(steam_id)",
            "ALTER TABLE announcements ADD COLUMN server_num INTEGER NOT NULL DEFAULT 1",
            "CREATE TABLE IF NOT EXISTS server_message_templates (server_num INTEGER PRIMARY KEY, connect_template TEXT, disconnect_template TEXT, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS server_api_keys (server_num INTEGER PRIMARY KEY, api_key VARCHAR(128) NOT NULL, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS scheduled_restarts (server_num INTEGER PRIMARY KEY, restart_at DATETIME)",
            "ALTER TABLE scheduled_restarts ADD COLUMN daily_restart_time VARCHAR(8) DEFAULT NULL",
            "CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY, server_num INTEGER NOT NULL DEFAULT 1, steam_id VARCHAR(32) NOT NULL, character_name VARCHAR(64) NOT NULL, reason VARCHAR(512) NOT NULL, admin_name VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_warnings_steam_id ON warnings(steam_id)",
            "CREATE TABLE IF NOT EXISTS bans (id INTEGER PRIMARY KEY, server_num INTEGER NOT NULL DEFAULT 1, steam_id VARCHAR(32) NOT NULL, character_name VARCHAR(64) NOT NULL, admin_name VARCHAR(64) NOT NULL, reason VARCHAR(512) NOT NULL, banned_at DATETIME NOT NULL, unban_at DATETIME, unbanned_at DATETIME)",
            "CREATE INDEX IF NOT EXISTS ix_bans_steam_id ON bans(steam_id)",
            "CREATE TABLE IF NOT EXISTS ban_appeals (id INTEGER PRIMARY KEY, ban_id INTEGER REFERENCES bans(id), steam_id VARCHAR(32) NOT NULL, character_name VARCHAR(64) NOT NULL, message VARCHAR(2000) NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'pending', admin_response VARCHAR(1024), admin_name VARCHAR(64), created_at DATETIME NOT NULL, resolved_at DATETIME)",
            "CREATE INDEX IF NOT EXISTS ix_ban_appeals_steam_id ON ban_appeals(steam_id)",
            "CREATE TABLE IF NOT EXISTS moderation_log (id INTEGER PRIMARY KEY, server_num INTEGER NOT NULL DEFAULT 1, action VARCHAR(32) NOT NULL, admin_name VARCHAR(64), target_name VARCHAR(64), target_steam_id VARCHAR(32), details VARCHAR(512), created_at DATETIME NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_moderation_log_created ON moderation_log(created_at)",
            "CREATE TABLE IF NOT EXISTS player_daily_activity (id INTEGER PRIMARY KEY, server_num INTEGER NOT NULL DEFAULT 1, steam_id VARCHAR(32) NOT NULL, activity_date VARCHAR(10) NOT NULL, UNIQUE(server_num, steam_id, activity_date))",
            "CREATE INDEX IF NOT EXISTS ix_player_daily_activity_steam_id ON player_daily_activity(steam_id)",
            # ─── Points economy ─────────────────────────────────────────────
            "ALTER TABLE users ADD COLUMN points_balance INTEGER NOT NULL DEFAULT 0",
            "CREATE TABLE IF NOT EXISTS points_transactions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, delta INTEGER NOT NULL, balance_after INTEGER NOT NULL, reason VARCHAR(32) NOT NULL, detail VARCHAR(256), ref_type VARCHAR(32), ref_id INTEGER, created_at DATETIME NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_points_transactions_user ON points_transactions(user_id, created_at)",
            "CREATE TABLE IF NOT EXISTS shop_items (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, description TEXT, cost INTEGER NOT NULL, image_url VARCHAR(512), is_active BOOLEAN NOT NULL DEFAULT 1, stock INTEGER, sort_order INTEGER NOT NULL DEFAULT 0, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
            "CREATE TABLE IF NOT EXISTS shop_redemptions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, shop_item_id INTEGER REFERENCES shop_items(id) ON DELETE SET NULL, item_name_snapshot VARCHAR(128) NOT NULL, cost_snapshot INTEGER NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'pending', delivery_mode VARCHAR(16) NOT NULL DEFAULT 'manual', player_note VARCHAR(500), admin_note VARCHAR(500), created_at DATETIME NOT NULL, resolved_at DATETIME, resolved_by VARCHAR(64))",
            "CREATE INDEX IF NOT EXISTS ix_shop_redemptions_status_created ON shop_redemptions(status, created_at)",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _seed_defaults(db)
        await _migrate_admin_role_tiers(db)
        # Restore maintenance flag file on startup
        try:
            res = await db.execute(select(Setting).where(Setting.key == "maintenance_mode"))
            s = res.scalar_one_or_none()
            _write_maintenance_flag(s is not None and s.value == "true")
        except Exception:
            pass
        # Pre-populate monitor history from DB snapshots (last 24h)
        from .monitor import init_history
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        cfg_res = await db.execute(
            select(Setting).where(Setting.key.in_(["server_ip", "server_port", "server2_ip", "server2_port"]))
        )
        srv_cfg = {s.key: s.value for s in cfg_res.scalars().all()}
        for srv_num, ip_key, port_key in [(1, "server_ip", "server_port"), (2, "server2_ip", "server2_port")]:
            ip = srv_cfg.get(ip_key, "").strip()
            port_str = srv_cfg.get(port_key, "27016") or "27016"
            if not ip or ip in ("127.0.0.1", "0.0.0.0"):
                continue
            port = int(port_str) if port_str.isdigit() else 27016
            snaps_res = await db.execute(
                select(ServerSnapshot)
                .where(ServerSnapshot.server_num == srv_num, ServerSnapshot.recorded_at >= cutoff)
                .order_by(ServerSnapshot.recorded_at.asc())
            )
            snaps = snaps_res.scalars().all()
            if snaps:
                init_history(ip, port, [(_utc_ts(s.recorded_at), s.players) for s in snaps])

    # Start background tasks
    task_publish = asyncio.create_task(_scheduled_publish_task())
    task_backup = asyncio.create_task(_auto_backup_task())
    task_cleanup = asyncio.create_task(_cleanup_task())
    task_monitor = asyncio.create_task(_monitor_poll_task())
    task_scheduler = asyncio.create_task(_scheduler_task())
    task_ranksnap = asyncio.create_task(_leaderboard_snapshot_task())
    yield
    task_publish.cancel()
    task_backup.cancel()
    task_cleanup.cancel()
    task_monitor.cancel()
    task_scheduler.cancel()
    task_ranksnap.cancel()


app = FastAPI(title="V Rising Server Site", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
_origins = [o.strip() for o in _raw_origins.split(",")] if _raw_origins != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


import hashlib
from starlette.middleware.base import BaseHTTPMiddleware


class PageViewMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (
            request.method == "GET"
            and not path.startswith("/api/")
            and not path.startswith("/uploads/")
            and "." not in path.split("/")[-1]
        ):
            try:
                ip = request.client.host if request.client else ""
                ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else None
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    db.add(PageView(path=path or "/", ip_hash=ip_hash))
                    await db.commit()
            except Exception:
                pass
        if response.status_code >= 500 and path.startswith("/api/"):
            try:
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    db.add(ErrorLog(
                        path=path, method=request.method,
                        status_code=response.status_code, error=None,
                    ))
                    await db.commit()
            except Exception:
                pass
        return response


app.add_middleware(PageViewMiddleware)


from .routers import points_shop, wipes

app.include_router(points_shop.router)
app.include_router(wipes.router)


# ─── Version ────────────────────────────────────────────────────────────────

@app.get("/api/version")
async def get_version():
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return {"version": version_file.read_text().strip()}
    return {"version": None}


# ─── SEO ─────────────────────────────────────────────────────────────────────

@app.get("/google{code}.html", response_class=Response)
async def google_verify(code: str, db: AsyncSession = Depends(get_db)):
    """Serves Google Search Console HTML verification file if key matches setting."""
    result = await db.execute(select(Setting).where(Setting.key == "google_site_verification_file"))
    s = result.scalar_one_or_none()
    if not s or s.value.strip() != code.strip():
        return Response(status_code=404)
    return Response(content=f"google-site-verification: google{code}.html", media_type="text/html")


# ─── Sitemap ─────────────────────────────────────────────────────────────────

@app.get("/api/sitemap.xml", response_class=Response)
async def sitemap(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(News.slug, News.updated_at).where(News.published == True).order_by(News.updated_at.desc())
    )
    slugs = result.all()
    base = str(request.base_url).rstrip("/")
    urls = [
        f"  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{base}/servers.html</loc><changefreq>hourly</changefreq><priority>0.7</priority></url>",
        f"  <url><loc>{base}/leaderboard.html</loc><changefreq>daily</changefreq><priority>0.6</priority></url>",
        f"  <url><loc>{base}/clans.html</loc><changefreq>daily</changefreq><priority>0.5</priority></url>",
        f"  <url><loc>{base}/map.html</loc><changefreq>monthly</changefreq><priority>0.4</priority></url>",
        f"  <url><loc>{base}/faq.html</loc><changefreq>monthly</changefreq><priority>0.4</priority></url>",
        f"  <url><loc>{base}/bans.html</loc><changefreq>weekly</changefreq><priority>0.4</priority></url>",
        f"  <url><loc>{base}/events.html</loc><changefreq>daily</changefreq><priority>0.6</priority></url>",
    ]
    for slug, updated_at in slugs:
        lastmod = updated_at.strftime("%Y-%m-%d") if updated_at else ""
        urls.append(f"  <url><loc>{base}/?news={slug}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += "\n".join(urls) + "\n</urlset>"
    return Response(content=xml, media_type="application/xml")


@app.get("/api/rss.xml")
async def rss_feed(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(News).options(selectinload(News.author))
        .where(News.published == True)
        .order_by(News.created_at.desc())
        .limit(20)
    )
    news_items = result.scalars().all()

    base_url = "https://v.just-skill.ru"
    # Try to read site URL from settings
    try:
        su_res = await db.execute(select(Setting).where(Setting.key == "https_domain"))
        su = su_res.scalar_one_or_none()
        if su and su.value.strip():
            base_url = f"https://{su.value.strip()}"
    except Exception:
        pass

    _strip_html = re.compile(r"<[^>]+>")
    items_xml = ""
    for n in news_items:
        title = (n.title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        desc = _strip_html.sub("", n.content or "")[:300].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        link = f"{base_url}/?news={n.slug}"
        pub_date = ""
        if n.created_at:
            dt = n.created_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        items_xml += f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <description>{desc}</description>
      <pubDate>{pub_date}</pubDate>
      <guid>{link}</guid>
    </item>"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>V Rising — Новости</title>
    <link>{base_url}</link>
    <description>Последние новости игрового сервера V Rising</description>
    <language>ru</language>
    <ttl>30</ttl>{items_xml}
  </channel>
</rss>"""
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


_NEWS_EMBED_META_PATTERNS = [
    (re.compile(r'(<title id="page-title">).*?(</title>)'), "title"),
    (re.compile(r'(<meta id="meta-description"[^>]*content=")[^"]*(")'), "desc"),
    (re.compile(r'(<link rel="canonical" href=")[^"]*(")'), "url"),
    (re.compile(r'(<meta property="og:url" content=")[^"]*(")'), "url"),
    (re.compile(r'(<meta id="meta-og-title"[^>]*content=")[^"]*(")'), "title"),
    (re.compile(r'(<meta id="meta-og-description"[^>]*content=")[^"]*(")'), "desc"),
    (re.compile(r'(<meta property="og:image" content=")[^"]*(")'), "image"),
    (re.compile(r'(<meta property="og:type" content=")[^"]*(")'), "article_type"),
    (re.compile(r'(<meta id="meta-tw-title"[^>]*content=")[^"]*(")'), "title"),
    (re.compile(r'(<meta id="meta-tw-description"[^>]*content=")[^"]*(")'), "desc"),
]


@app.get("/api/news-embed")
async def news_embed(slug: str, db: AsyncSession = Depends(get_db)):
    """Server-rendered <head> meta for one article, for crawlers that don't run JS
    (Discord/Telegram/VK/Twitter link-unfurlers, most search bots) — they never see
    index.js's client-side setMeta() call, so a shared article link previously showed
    the generic homepage title/description/image no matter which article it was.
    nginx (see nginx-ssl.conf's $is_crawler_ua map) routes just those user-agents hitting
    "/?news=<slug>" here instead of the static index.html; everyone else still gets the
    plain SPA. Re-uses frontend/index.html itself (read from the repo mount at
    /opt/vrising-site) so layout/styling never drifts out of sync — only the meta tag
    values are swapped before serving.
    """
    try:
        with open(_INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            page = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail="index.html not found")

    result = await db.execute(
        select(News).options(selectinload(News.author)).where(News.slug == slug, News.published == True)
    )
    news = result.scalar_one_or_none()
    if news is None:
        return Response(content=page, media_type="text/html; charset=utf-8")

    base_url = "https://v.just-skill.ru"
    try:
        su_res = await db.execute(select(Setting).where(Setting.key == "https_domain"))
        su = su_res.scalar_one_or_none()
        if su and su.value.strip():
            base_url = f"https://{su.value.strip()}"
    except Exception:
        pass

    image = news.thumbnail_url or f"{base_url}/uploads/og-default.png"
    if image.startswith("/"):
        image = base_url + image
    plain_desc = re.sub(r"<[^>]+>", "", news.summary or news.content or "").strip()[:160]

    values = {
        "title": html.escape(f"{news.title} — Just-Skill.Ru"),
        "desc": html.escape(plain_desc),
        "url": html.escape(f"{base_url}/?news={news.slug}"),
        "image": html.escape(image),
        "article_type": "article",
    }
    for pattern, key in _NEWS_EMBED_META_PATTERNS:
        page = pattern.sub(lambda m, v=values[key]: m.group(1) + v + m.group(2), page, count=1)

    # NewsArticle structured data — makes the article eligible for Google News /
    # rich-result treatment; the static page only ever carries an Organization schema.
    def _iso(dt):
        if dt is None:
            return None
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat()

    jsonld = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": news.title,
        "description": plain_desc,
        "image": [image],
        "datePublished": _iso(news.created_at),
        "dateModified": _iso(news.updated_at) or _iso(news.created_at),
        "author": {"@type": "Person", "name": news.author.username},
        "mainEntityOfPage": f"{base_url}/?news={news.slug}",
    }
    jsonld_tag = f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>\n</head>'
    page = page.replace("</head>", jsonld_tag, 1)

    return Response(content=page, media_type="text/html; charset=utf-8")


# ─── Setup ──────────────────────────────────────────────────────────────────

@app.get("/api/setup/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting).where(Setting.key == "setup_completed"))
    s = result.scalar_one_or_none()
    if s and s.value == "true":
        return {"completed": True}
    admin_result = await db.execute(select(User).where(User.role.in_(("admin", "superadmin"))).limit(1))
    if admin_result.scalar_one_or_none():
        return {"completed": True}
    return {"completed": False}


@app.post("/api/setup/complete", response_model=TokenOut, status_code=201)
async def setup_complete(body: SetupComplete, response: Response, db: AsyncSession = Depends(get_db)):
    sc_result = await db.execute(select(Setting).where(Setting.key == "setup_completed"))
    sc = sc_result.scalar_one_or_none()
    admin_result = await db.execute(select(User).where(User.role.in_(("admin", "superadmin"))).limit(1))
    if (sc and sc.value == "true") or admin_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Setup already completed")
    existing = await db.execute(select(User).where(
        (User.username == body.username) | (User.email == body.email)
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username or email already taken")
    # Founding account is superadmin, not plain admin — a fresh install must bootstrap an
    # owner with full capability (backups/rcon/ssl/role-management) from day one, matching
    # what the one-time migration does for pre-existing installs (see role_tiers migration).
    admin = User(
        username=body.username,
        email=body.email,
        hashed_password=get_password_hash(body.password),
        role="superadmin",
    )
    db.add(admin)
    await db.flush()
    if sc:
        sc.value = "true"
        sc.updated_at = datetime.now(timezone.utc)
    else:
        db.add(Setting(key="setup_completed", value="true"))
    welcome = News(
        title="Добро пожаловать на сервер!",
        slug="dobro-pozhalovat-na-server",
        summary="Официальный сайт нашего сервера V Rising запущен.",
        content="Официальный сайт нашего сервера V Rising запущен.\n\nЗдесь вы найдёте последние новости, статус сервера и многое другое.\n\nПриятной игры!",
        thumbnail_url=None,
        author_id=admin.id,
        published=True,
    )
    db.add(welcome)
    await db.commit()
    await db.refresh(admin)
    token = create_access_token({"sub": str(admin.id)})
    _set_auth_cookie(response, token)
    return TokenOut(access_token=token, user=UserOut.model_validate(admin))


# ─── Castle Overseer Chat ────────────────────────────────────────────────────

@app.post("/api/chat")
@limiter.limit("20/minute")
async def castle_overseer_chat(request: Request, body: ChatRequest):
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="Управляющий замком сейчас недоступен. Добавьте ANTHROPIC_API_KEY в .env")
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)
        messages = [
            {"role": h.role, "content": h.content}
            for h in body.history[-10:]
            if h.role in ("user", "assistant")
        ]
        messages.append({"role": "user", "content": body.message})

        async def generate():
            try:
                async with client.messages.stream(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    system=OVERSEER_PROMPT,
                    messages=messages,
                ) as stream:
                    async for text in stream.text_stream:
                        yield f"data: {json.dumps({'text': text})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except ImportError:
        raise HTTPException(status_code=503, detail="Библиотека anthropic не установлена")


# ─── Auth ───────────────────────────────────────────────────────────────────

@app.post("/api/auth/register", response_model=TokenOut, status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, body: UserRegister, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(
        (User.username == body.username) | (User.email == body.email)
    ))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username or email already taken")
    nick = (body.game_nickname or "").strip()[:64] or None
    user = User(
        username=body.username,
        email=body.email,
        hashed_password=get_password_hash(body.password),
        role="user",
        game_nickname=nick,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token({"sub": str(user.id)})
    _set_auth_cookie(response, token)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@app.post("/api/auth/login", response_model=TokenOut)
@limiter.limit("10/minute")
async def login(request: Request, body: UserLogin, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Ваш аккаунт был заблокирован.")
    if user.totp_enabled:
        import pyotp
        if not body.totp_code or not pyotp.TOTP(user.totp_secret).verify(body.totp_code, valid_window=1):
            raise HTTPException(status_code=401, detail="Требуется код 2FA")
    token = create_access_token({"sub": str(user.id)})
    _set_auth_cookie(response, token)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@app.post("/api/auth/logout", status_code=204)
async def logout(response: Response, current_user: User = Depends(get_current_user), request: Request = None, db: AsyncSession = Depends(get_db)):
    auth_header = request.headers.get("Authorization", "") if request else ""
    cookie_token = request.cookies.get(COOKIE_NAME, "") if request else ""
    token = auth_header[7:] if auth_header.startswith("Bearer ") else cookie_token
    # Stamp last_active_at at logout so "last seen" is accurate
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one_or_none()
    if user:
        user.last_active_at = datetime.now(timezone.utc)
        await db.commit()
    # Remove from online tracking immediately
    _explicit_logouts[current_user.username] = time.time()
    for vid in list(_visitor_data):
        if _visitor_data[vid].get("username") == current_user.username:
            del _visitor_data[vid]
    if token:
        await revoke_token(token, db)
    _clear_auth_cookie(response)


@app.get("/api/auth/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    last = current_user.last_active_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if not last or (now - last).total_seconds() > 60:
        current_user.last_active_at = now
        await db.commit()
    return UserOut.model_validate(current_user)


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

@app.get("/api/plugin/status")
@limiter.limit("60/minute")
async def plugin_status(
    request: Request,
    steam_id: str,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Checked by the plugin on player connect to decide whether to show the
    "you're not registered yet" chat message (or, if already linked, a
    "logged in as {username}" welcome message)."""
    result = await db.execute(
        select(User.username, User.rules_accepted_at).where(User.steam_id == steam_id)
    )
    row = result.first()
    if row is None:
        return {"registered": False, "username": None, "rules_accepted": None}
    username, rules_accepted_at = row
    return {
        "registered": True,
        "username": username,
        "rules_accepted": rules_accepted_at is not None,
    }


@app.get("/api/plugin/rules")
@limiter.limit("60/minute")
async def plugin_get_rules(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Returns the server rules (site admin-managed, Setting "rules") so the plugin can
    show them to a freshly-registered player and prompt for in-game acceptance. Rules
    aren't actually per-server, but server_num is still accepted/required like the other
    plugin GET endpoints for consistent API-key resolution in _require_plugin_key."""
    result = await db.execute(select(Setting).where(Setting.key == "rules"))
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return {"rules": []}
    try:
        rules = json.loads(setting.value)
    except (TypeError, ValueError):
        rules = []
    return {"rules": rules}


@app.post("/api/plugin/accept-rules")
@limiter.limit("30/minute")
async def plugin_accept_rules(
    request: Request,
    body: PluginAcceptRules,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game rules-acceptance prompt — mirrors POST /api/auth/accept-rules
    for website users, but keyed by steam_id since there's no JWT session in-game."""
    result = await db.execute(select(User).where(User.steam_id == body.steam_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="not_registered")
    if user.rules_accepted_at is None:
        user.rules_accepted_at = datetime.now(timezone.utc)
        await db.commit()
    return {"success": True}


@app.post("/api/plugin/register")
@limiter.limit("10/minute")
async def plugin_register(
    request: Request,
    body: PluginRegister,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .register <password> command. Username is the game character
    name (not a separate field the player types); email is a synthesized placeholder
    since chat commands have nowhere to collect a real one — the player can set a real
    email later from their site profile."""
    existing_steam = await db.execute(select(User.id).where(User.steam_id == body.steam_id))
    if existing_steam.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="already_registered")

    username = body.character_name.strip()[:32]
    if not re.match(r"^[a-zA-Z0-9_а-яёА-ЯЁ ]{3,32}$", username):
        raise HTTPException(status_code=400, detail="invalid_username")

    existing_username = await db.execute(select(User.id).where(User.username == username))
    if existing_username.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="username_taken")

    user = User(
        username=username,
        email=f"steam_{body.steam_id}@vrising.local",
        hashed_password=get_password_hash(body.password),
        role="user",
        game_nickname=username,
        steam_id=body.steam_id,
    )
    db.add(user)
    await db.commit()
    return {"success": True, "username": username}


@app.post("/api/plugin/login")
@limiter.limit("10/minute")
async def plugin_login(
    request: Request,
    body: PluginLogin,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .login <password> command — links steam_id to an existing
    site account (e.g. one created via the website) after verifying credentials, for
    players who already have a site account under their character name."""
    username = body.character_name.strip()[:32]
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="bad_credentials")
    if user.steam_id and user.steam_id != body.steam_id:
        raise HTTPException(status_code=409, detail="linked_elsewhere")
    if user.steam_id != body.steam_id:
        user.steam_id = body.steam_id
        await db.commit()
    return {"success": True, "username": user.username}


@app.post("/api/plugin/heartbeat")
@limiter.limit("120/minute")
async def plugin_heartbeat(
    request: Request,
    body: PluginHeartbeatIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Sent periodically (~every 60s) by the plugin so the admin panel can show a
    connection-status pill (see GET /api/admin/plugin-status). Upserts the single
    row for this server_num — no history is kept, just the latest snapshot."""
    result = await db.execute(select(PluginHeartbeat).where(PluginHeartbeat.server_num == body.server_num))
    hb = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if hb is None:
        hb = PluginHeartbeat(server_num=body.server_num)
        db.add(hb)
    hb.server_name = body.server_name
    hb.plugin_version = body.plugin_version
    hb.player_count = body.player_count
    hb.last_seen_at = now
    await db.commit()
    return {"success": True}


@app.post("/api/plugin/sessions")
@limiter.limit("60/minute")
async def plugin_report_session(
    request: Request,
    body: PluginSessionReport,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Sent by the plugin's SessionTracker on player disconnect, reporting the session
    that just ended. This is the accuracy source for playtime, replacing A2S polling
    wherever the plugin is running (monitor.py's polling keeps running as a fallback
    for servers without the plugin). Upsert/merge logic, in order:
      1. Match an existing PlayerRecord by (server_num, steam_id) — the stable identity
         once a row has been claimed by a previous report.
      2. Else match an existing A2S-only PlayerRecord by (server_num, player_name,
         steam_id IS NULL) — a row accumulated purely from passive polling that has
         never been claimed by a verified session report yet. Claim it (set steam_id)
         instead of creating a duplicate, preserving its prior total_seconds.
      3. Else create a brand new PlayerRecord for this steam_id.
    In all cases, player_name is refreshed (character rename), and the session's
    seconds/last_seen/session_count are applied on top."""
    result = await db.execute(
        select(PlayerRecord).where(
            PlayerRecord.server_num == body.server_num,
            PlayerRecord.steam_id == body.steam_id,
        )
    )
    record = result.scalar_one_or_none()

    if record is None:
        claim_result = await db.execute(
            select(PlayerRecord).where(
                PlayerRecord.server_num == body.server_num,
                PlayerRecord.player_name == body.character_name,
                PlayerRecord.steam_id.is_(None),
            )
        )
        record = claim_result.scalar_one_or_none()
        if record is not None:
            record.steam_id = body.steam_id

    if record is None:
        record = PlayerRecord(
            server_num=body.server_num,
            player_name=body.character_name,
            steam_id=body.steam_id,
            total_seconds=0,
            session_count=0,
        )
        db.add(record)

    if body.ended_at is not None:
        ended_at = body.ended_at
        if ended_at.tzinfo is not None:
            ended_at = ended_at.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        ended_at = datetime.now(timezone.utc).replace(tzinfo=None)

    record.player_name = body.character_name
    record.total_seconds += body.session_seconds
    record.last_duration = body.session_seconds
    record.last_seen = ended_at
    record.session_count += 1

    # Playtime-earning hook — awards points to the linked site account, if any. An A2S-only
    # session (no site account has claimed this steam_id) is a no-op; PlayerRecord update
    # above proceeds unchanged either way.
    points_cfg = await _get_points_config(db)
    earned = (body.session_seconds // 60) * points_cfg["per_minute"]
    if earned > 0:
        user_res = await db.execute(select(User).where(User.steam_id == body.steam_id))
        earning_user = user_res.scalar_one_or_none()
        if earning_user is not None:
            await _award_points(db, earning_user, earned, "playtime", f"{body.session_seconds}s session on server {body.server_num}")

    await db.commit()
    return {"success": True}


@app.post("/api/plugin/clans/sync")
@limiter.limit("30/minute")
async def plugin_clans_sync(
    request: Request,
    body: PluginClansSyncIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Sent periodically by the plugin with the FULL current in-game clan roster for one
    server. This replaces (not merges) all game_clans rows for body.server_num — the
    plugin always reports its complete current state, so a stale clan that no longer
    exists in-game simply won't be in the next payload and gets dropped here.

    IMPORTANT: GameClanMember.clan_id declares ondelete="CASCADE" at the ORM level, but
    that is NOT actually enforced by the live DB — SQLite only applies ON DELETE CASCADE
    when a connection has run `PRAGMA foreign_keys = ON`, and this app's engine
    (backend/database.py) never sets that pragma, so the cascade is silently a no-op.
    Deleting a GameClan row without also explicitly deleting its GameClanMember rows
    leaves them orphaned-but-still-clan_id-matching, and since SQLite's plain
    `INTEGER PRIMARY KEY` (no AUTOINCREMENT) reuses the lowest free rowid, the next insert
    can get the SAME id, causing every previous cycle's "orphaned" members to silently
    reattach and pile up. So we must delete members explicitly, scoped by clan id, before
    deleting the clans themselves. See models.py for the same note near the FK column."""
    clan_ids_result = await db.execute(
        select(GameClan.id).where(GameClan.server_num == body.server_num)
    )
    clan_ids = [row[0] for row in clan_ids_result.all()]
    if clan_ids:
        await db.execute(
            delete(GameClanMember).where(GameClanMember.clan_id.in_(clan_ids))
        )
    await db.execute(
        delete(GameClan).where(GameClan.server_num == body.server_num)
    )
    for clan_in in body.clans:
        clan = GameClan(
            server_num=body.server_num,
            clan_guid=clan_in.clan_guid,
            name=clan_in.name,
            motto=clan_in.motto or "",
        )
        db.add(clan)
        await db.flush()
        for member_in in clan_in.members:
            db.add(GameClanMember(
                clan_id=clan.id,
                steam_id=member_in.steam_id,
                character_name=member_in.character_name,
                role=member_in.role,
            ))
    await db.commit()
    return {"success": True, "clan_count": len(body.clans)}


@app.get("/api/plugin/announcements")
@limiter.limit("120/minute")
async def plugin_get_announcements(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (piggybacking on its heartbeat interval, ~every 60s) so the
    admin can push scheduled/recurring in-game chat announcements from the site without
    restarting the server/plugin. Replaces the old single-text server_announcement Setting
    with a proper Announcement table (see the "Scheduled Announcements" admin section
    below) supporting one-off and recurring messages. A row is "due" if it's never been
    sent, or (for recurring rows) if interval_minutes have elapsed since last_sent_at —
    once a due row is returned here, last_sent_at is stamped immediately, since the plugin
    is trusted to broadcast on receipt (same resilience level as the rest of this
    integration — no separate delivery-ack round-trip). server_num scopes the poll to a
    single game server (each plugin instance sends its own server_num, matching its config)
    — defaults to 1 for backward compat with an old plugin build that predates per-server
    announcements, but the current plugin always sends it explicitly. Response shape:
    {"announcements": [{"text": str, "target_steam_id": str|null}, ...]} — target_steam_id
    is null for normal (broadcast-to-everyone) rows and a SteamID for one-off test-sends
    created via POST /api/admin/announcements/test-send, which the plugin should deliver
    only to that player instead of broadcasting."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Announcement)
        .where(
            Announcement.enabled == True,  # noqa: E712
            Announcement.server_num == server_num,
            or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
        )
        .order_by(Announcement.created_at.asc())
    )
    rows = result.scalars().all()

    due: list[Announcement] = []
    for a in rows:
        if a.last_sent_at is None:
            due.append(a)
            continue
        if a.interval_minutes is None:
            continue  # one-off announcement already sent — never due again
        last_sent = a.last_sent_at
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        if (now - last_sent).total_seconds() >= a.interval_minutes * 60:
            due.append(a)

    for a in due:
        a.last_sent_at = now
    if due:
        await db.commit()

    return {"announcements": [{"text": a.text, "target_steam_id": a.target_steam_id} for a in due]}


@app.get("/api/plugin/message-templates", response_model=ServerMessageTemplateOut)
@limiter.limit("120/minute")
async def plugin_get_message_templates(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (same cadence as its heartbeat) so the site can drive the
    connect/disconnect in-game chat message text — per-server ServerMessageTemplate rows,
    replacing the old global "connect_message_template"/"disconnect_message_template"
    Settings now that the plugin runs on more than one server — without editing the
    plugin's local BepInEx .cfg and restarting the game server. server_num defaults to 1
    for backward compat with an old plugin build; the current plugin always sends it
    explicitly. Empty string means "not set"; the plugin falls back to its own local
    config default in that case. Response shape: {"connect": str, "disconnect": str}."""
    result = await db.execute(
        select(ServerMessageTemplate).where(ServerMessageTemplate.server_num == server_num)
    )
    row = result.scalar_one_or_none()
    return ServerMessageTemplateOut(
        connect=(row.connect_template or "") if row else "",
        disconnect=(row.disconnect_template or "") if row else "",
    )


# ─── Wipe countdown (plugin) ────────────────────────────────────────────────────
# Backed by the same wipe_date/wipe_type (server 1) and wipe_date2/wipe_type2 (server 2)
# Settings the admin panel's "Вайп" card already writes via a raw <input
# type="datetime-local"> — the stored value is local wall-clock time in the site's
# configured timezone (Setting "timezone"), e.g. "2024-01-15T18:30", NOT UTC.

@app.get("/api/plugin/wipe-info")
@limiter.limit("60/minute")
async def plugin_wipe_info(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Upcoming-wipe countdown for the in-game plugin. Response shape:
    {"wipe_date": "<ISO-8601 UTC, 'Z' suffix>" | null, "wipe_type": str | null}.
    Never raises for a bad/missing value — an unrecognized server_num, an empty/unset
    wipe_date, or an unparseable stored value all just come back as null/null, since this
    is a best-effort in-game countdown display."""
    if server_num == 1:
        date_key, type_key = "wipe_date", "wipe_type"
    elif server_num == 2:
        date_key, type_key = "wipe_date2", "wipe_type2"
    else:
        return {"wipe_date": None, "wipe_type": None}

    result = await db.execute(select(Setting).where(Setting.key.in_([date_key, type_key])))
    settings = {s.key: s.value for s in result.scalars().all()}
    raw_date = (settings.get(date_key) or "").strip()
    if not raw_date:
        return {"wipe_date": None, "wipe_type": None}

    try:
        local_dt = datetime.fromisoformat(raw_date)
    except ValueError:
        return {"wipe_date": None, "wipe_type": None}

    tz = await _site_timezone(db)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    return {"wipe_date": _fmt_dt_z(utc_dt), "wipe_type": settings.get(type_key) or None}


# ─── Player playtime (plugin) ───────────────────────────────────────────────────
# Global (all-servers) total playtime for a linked SteamID, queried directly off
# PlayerRecord.steam_id (set once the plugin reports a real session for that row — see
# PlayerRecord.steam_id's comment in models.py) — no join through User needed since that
# column is already populated by verified session reports. Mirrors the same
# func.sum(PlayerRecord.total_seconds) aggregation used for a player's public profile
# total (GET /api/users/{username}), which likewise sums across every server_num rather
# than scoping to one — this endpoint does the same for consistency.

@app.get("/api/plugin/playtime")
@limiter.limit("60/minute")
async def plugin_playtime(
    request: Request,
    steam_id: str,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """server_num is accepted only for the usual plugin-key resolution in
    _require_plugin_key — the returned total is always global across all servers.
    Response: {"total_seconds": int}, 0 (never null) for an unknown/unregistered
    steam_id."""
    result = await db.execute(
        select(func.sum(PlayerRecord.total_seconds)).where(PlayerRecord.steam_id == steam_id)
    )
    total_seconds = int(result.scalar_one() or 0)
    return {"total_seconds": total_seconds}


# ─── Connect streak (plugin) ────────────────────────────────────────────────────
# Tracks, per player per server, which CALENDAR DAYS (in the site's configured timezone —
# Setting "timezone", same _site_timezone helper as wipe-info/daily-restart above) a
# player connected at least once, so the plugin can show a "you've played N days in a
# row!" message on connect. See PlayerDailyActivity in models.py.

@app.post("/api/plugin/connect-streak")
@limiter.limit("60/minute")
async def plugin_connect_streak(
    request: Request,
    body: PluginConnectStreakIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Records today (site-local calendar date) as active for this steam_id+server_num if
    not already recorded — idempotent, a player connecting more than once in the same day
    doesn't double-insert (PlayerDailyActivity's unique constraint) or inflate the streak —
    then computes the current consecutive-day streak by walking backward one day at a time
    from today until the first day with no activity row. Response: {"streak_days": N},
    N >= 1 since today was just recorded."""
    tz = await _site_timezone(db)
    today = datetime.now(tz).date()
    today_str = today.isoformat()

    existing = await db.execute(
        select(PlayerDailyActivity).where(
            PlayerDailyActivity.server_num == body.server_num,
            PlayerDailyActivity.steam_id == body.steam_id,
            PlayerDailyActivity.activity_date == today_str,
        )
    )
    was_new_today = existing.scalar_one_or_none() is None
    if was_new_today:
        db.add(PlayerDailyActivity(
            server_num=body.server_num,
            steam_id=body.steam_id,
            activity_date=today_str,
        ))
        await db.commit()

    dates_result = await db.execute(
        select(PlayerDailyActivity.activity_date).where(
            PlayerDailyActivity.server_num == body.server_num,
            PlayerDailyActivity.steam_id == body.steam_id,
        )
    )
    active_dates = {row[0] for row in dates_result.all()}

    streak_days = 0
    cursor = today
    while cursor.isoformat() in active_dates:
        streak_days += 1
        cursor -= timedelta(days=1)

    # Streak-bonus earning hook — only on the first connect of the day (was_new_today),
    # never on a same-day idempotent re-poll, and only once the streak reaches the
    # configured minimum. Second, separate commit (streak_days isn't known until after
    # the first commit above already ran).
    points_cfg = await _get_points_config(db)
    if was_new_today and streak_days >= points_cfg["streak_min_days"]:
        user_res = await db.execute(select(User).where(User.steam_id == body.steam_id))
        earning_user = user_res.scalar_one_or_none()
        if earning_user is not None:
            await _award_points(db, earning_user, points_cfg["streak_bonus"], "streak", f"streak day {streak_days}")
            await db.commit()

    return {"streak_days": streak_days}


# ─── Scheduled server restart ──────────────────────────────────────────────────
# An admin sets "restart in N minutes" either from the site admin panel (POST/DELETE
# /api/admin/servers/{server_num}/restart, below in the admin section) or, separately,
# from an in-game admin chat command that hits the plugin-facing endpoints here — both
# paths share the ScheduledRestart row and the _schedule_restart/_cancel_restart helpers
# below so they can't get out of sync. The plugin polls GET .../restart-status (same
# cadence as its heartbeat) to know when to start broadcasting a countdown to players and
# when to actually execute the restart; it is expected to POST cancel-restart itself right
# after doing so, as cleanup — this endpoint makes no distinction between that call and an
# admin explicitly cancelling a pending restart.

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


def _next_daily_restart_utc(daily_restart_time: str, tz: ZoneInfo) -> datetime:
    """Next occurrence of "HH:MM" (interpreted in the site's configured timezone) as a
    naive UTC datetime — today at that time if it's still in the future relative to "now"
    in that timezone, otherwise tomorrow. Mirrors _schedule_restart's naive-UTC storage
    convention for ScheduledRestart.restart_at."""
    hour, minute = (int(p) for p in daily_restart_time.split(":"))
    now_local = datetime.now(tz)
    candidate_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate_local <= now_local:
        candidate_local += timedelta(days=1)
    return candidate_local.astimezone(timezone.utc).replace(tzinfo=None)


@app.get("/api/plugin/restart-status")
@limiter.limit("120/minute")
async def plugin_restart_status(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (same cadence as its heartbeat, ~every 60s). Response shape:
    {"restart_at": "<ISO-8601 UTC, 'Z' suffix>" | null} — null means no restart pending.

    Also self-arms a recurring daily_restart_time (see the "Recurring daily restart"
    section below): if restart_at is currently null but daily_restart_time is set for
    this server_num, computes the next occurrence and persists it into restart_at right
    here — as if an admin had just scheduled a one-off restart for that moment — so this
    poll (and every other reader of the row, including the admin panel) sees it
    immediately. No separate cron/background scheduler needed: once the plugin executes
    the restart and calls POST /api/plugin/cancel-restart (which clears restart_at but
    deliberately leaves daily_restart_time untouched), the very next poll here re-arms
    the following day's occurrence automatically."""
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    if row is not None and row.restart_at is None and row.daily_restart_time:
        tz = await _site_timezone(db)
        row.restart_at = _next_daily_restart_utc(row.daily_restart_time, tz)
        await db.commit()
    return {"restart_at": _fmt_dt_z(row.restart_at if row else None)}


@app.post("/api/plugin/schedule-restart")
@limiter.limit("30/minute")
async def plugin_schedule_restart(
    request: Request,
    body: PluginScheduleRestartIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs an in-game admin chat command (e.g. "restart in 10 minutes"). Overwrites
    any previously scheduled restart for body.server_num rather than stacking."""
    restart_at = await _schedule_restart(db, body.server_num, body.minutes)
    return {"restart_at": _fmt_dt_z(restart_at)}


@app.post("/api/plugin/cancel-restart")
@limiter.limit("30/minute")
async def plugin_cancel_restart(
    request: Request,
    body: PluginCancelRestartIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Idempotent — 200 with no error whether or not a restart was actually pending."""
    await _cancel_restart(db, body.server_num)
    return {"success": True}


@app.post("/api/plugin/warn")
@limiter.limit("30/minute")
async def plugin_warn(
    request: Request,
    body: PluginWarnIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .warn admin chat command. Logs a moderation warning against
    body.steam_id; warning_count is the player's total across all servers/time,
    including the one just inserted."""
    db.add(Warning(
        server_num=body.server_num,
        steam_id=body.steam_id,
        character_name=body.character_name,
        reason=body.reason,
        admin_name=body.admin_name,
        created_at=datetime.utcnow(),
    ))
    await db.commit()
    count_result = await db.execute(select(func.count()).where(Warning.steam_id == body.steam_id))
    warning_count = count_result.scalar_one()
    return {"success": True, "warning_count": warning_count}


@app.get("/api/plugin/warnings")
@limiter.limit("60/minute")
async def plugin_warnings(
    request: Request,
    steam_id: str,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Backs the in-game .warnings admin chat command — lists ALL warnings for
    steam_id across every server, most recent first. server_num is accepted only for
    the usual API-key resolution in _require_plugin_key; results are never filtered
    by it, since a player's warning history should follow them across servers."""
    result = await db.execute(
        select(Warning).where(Warning.steam_id == steam_id).order_by(Warning.created_at.desc())
    )
    warnings = result.scalars().all()
    return {
        "warnings": [
            {
                "reason": w.reason,
                "admin_name": w.admin_name,
                "created_at": _fmt_dt_z(w.created_at),
                "server_num": w.server_num,
            }
            for w in warnings
        ]
    }


# ─── Player bans (plugin) ──────────────────────────────────────────────────────
# Backs the in-game .ban/.unban admin chat commands. The game engine itself performs the
# real ban/unban (native ban events) — these routes are only site-side record-keeping plus
# auto-expiry scheduling for temp bans. See models.Ban's docstring for the full lifecycle.

@app.post("/api/plugin/ban")
@limiter.limit("30/minute")
async def plugin_ban(
    request: Request,
    body: PluginBanIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Logs a new ban (permanent if body.unban_at is null, temp otherwise) issued via the
    in-game .ban admin chat command. unban_at is normalized to naive UTC before storing
    (this repo's usual DateTime convention) regardless of what offset/format it arrived in."""
    unban_at = body.unban_at
    if unban_at is not None and unban_at.tzinfo is not None:
        unban_at = unban_at.astimezone(timezone.utc).replace(tzinfo=None)
    db.add(Ban(
        server_num=body.server_num,
        steam_id=body.steam_id,
        character_name=body.character_name,
        admin_name=body.admin_name,
        reason=body.reason,
        banned_at=datetime.utcnow(),
        unban_at=unban_at,
    ))
    await db.commit()
    return {"success": True}


@app.post("/api/plugin/unban")
@limiter.limit("30/minute")
async def plugin_unban(
    request: Request,
    body: PluginUnbanIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Called by the plugin right after it manually executes a .unban in-game. Resolves
    whatever active ban(s) exist for this steam_id across ALL servers (not just
    body.server_num) — matches the cross-server enforcement in GET /api/plugin/ban-status:
    a ban issued on one server now blocks connecting to every tracked server, so lifting it
    must clear it everywhere too, including when an admin runs .unban on a different server
    than the one that originally issued the ban. Idempotent — 200 even if nothing was
    active."""
    result = await db.execute(
        select(Ban).where(
            Ban.steam_id == body.steam_id,
            Ban.unbanned_at.is_(None),
        )
    )
    active = result.scalars().all()
    if active:
        now = datetime.utcnow()
        for b in active:
            b.unbanned_at = now
        await db.commit()
    return {"success": True}


@app.get("/api/plugin/due-unbans")
@limiter.limit("60/minute")
async def plugin_due_unbans(
    request: Request,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Polled by the plugin (same cadence as its heartbeat) so it knows which players to
    actually unban in-game. Returns active bans (unbanned_at IS NULL) for server_num whose
    unban_at has passed — covers BOTH a temp ban's timer naturally expiring AND an admin
    force-unbanning from the site's bans admin page (which just sets unban_at to "now").
    Same "returning due items also consumes them" pattern as GET /api/plugin/announcements
    above: each due row is stamped unbanned_at immediately, since the plugin is trusted to
    actually execute the unban on receipt. Never returns permanent bans (unban_at NULL)."""
    now = datetime.utcnow()
    result = await db.execute(
        select(Ban).where(
            Ban.server_num == server_num,
            Ban.unbanned_at.is_(None),
            Ban.unban_at.isnot(None),
            Ban.unban_at <= now,
        )
    )
    due = result.scalars().all()
    for b in due:
        b.unbanned_at = now
    if due:
        await db.commit()
    return {"unbans": [{"steam_id": b.steam_id, "character_name": b.character_name} for b in due]}


@app.get("/api/plugin/ban-status")
@limiter.limit("60/minute")
async def plugin_ban_status(
    request: Request,
    steam_id: str,
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Checked by the plugin on every player connect as a workaround for the game
    engine's own native ban enforcement not reliably rejecting an already-banned player.
    Cross-server: a ban issued on ANY of the site's tracked servers now blocks connecting
    to ALL of them — server_num is accepted for backward compatibility but no longer used
    to filter; looks up any currently-active Ban (unbanned_at IS NULL) for this steam_id
    regardless of which server originally issued it. If more than one is somehow active at
    once, a permanent one wins over a temporary one, then the most recently issued.
    unban_at NULL in the response means a permanent ban."""
    result = await db.execute(
        select(Ban)
        .where(Ban.steam_id == steam_id, Ban.unbanned_at.is_(None))
        .order_by(Ban.unban_at.is_(None).desc(), Ban.banned_at.desc())
    )
    ban = result.scalars().first()
    if ban is None:
        return {"banned": False}
    return {
        "banned": True,
        "admin_name": ban.admin_name,
        "reason": ban.reason,
        "unban_at": _fmt_dt_z(ban.unban_at),
    }


_VALID_LOG_ACTIONS = {"kick", "mute", "unmute", "restart_scheduled", "restart_executed"}


@app.post("/api/plugin/log-action")
@limiter.limit("60/minute")
async def plugin_log_action(
    request: Request,
    body: PluginLogActionIn,
    db: AsyncSession = Depends(get_db),
    _key: None = Depends(_require_plugin_key),
):
    """Records the moderation action types NOT already covered by their own dedicated
    endpoints — ban/unban -> POST /api/plugin/ban /unban, warn -> POST /api/plugin/warn —
    for the unified feed at GET /api/admin/moderation-log. Only the 5 values in
    _VALID_LOG_ACTIONS are accepted (400 "invalid_action" otherwise) to avoid
    double-counting ban/unban/warn once merged into that feed."""
    if body.action not in _VALID_LOG_ACTIONS:
        raise HTTPException(status_code=400, detail="invalid_action")
    db.add(ModerationLogEntry(
        server_num=body.server_num,
        action=body.action,
        admin_name=body.admin_name,
        target_name=body.target_name,
        target_steam_id=body.target_steam_id,
        details=body.details,
        created_at=datetime.utcnow(),
    ))
    await db.commit()
    return {"success": True}


@app.get("/api/admin/plugin-status", response_model=list[PluginHeartbeatOut])
async def get_plugin_status(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(PluginHeartbeat).order_by(PluginHeartbeat.server_num))
    return [PluginHeartbeatOut.model_validate(h) for h in result.scalars().all()]


# ─── Scheduled Announcements ───────────────────────────────────────────────────
# Admin-managed in-game chat announcements, polled by the plugin via
# GET /api/plugin/announcements above. Replaces the old single-text
# "server_announcement" Setting (kept in ALLOWED_SETTING_KEYS/seed defaults as unused
# dead schema, same call as GameClan's note about Clan — not worth a migration to purge).

@app.get("/api/admin/announcements", response_model=list[AnnouncementOut])
async def list_announcements(
    server_num: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    # Exclude one-off test-sends (target_steam_id set) — they're single-use, self-expiring
    # (see /test-send below) and would just clutter the management table. server_num is
    # optional here for backward compat (omit = all servers); the admin UI always passes it.
    filters = [Announcement.target_steam_id.is_(None)]
    if server_num is not None:
        filters.append(Announcement.server_num == server_num)
    result = await db.execute(
        select(Announcement)
        .where(*filters)
        .order_by(Announcement.created_at.desc())
    )
    return [AnnouncementOut.model_validate(a) for a in result.scalars().all()]


@app.post("/api/admin/announcements", response_model=AnnouncementOut, status_code=201)
async def create_announcement(
    body: AnnouncementCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    a = Announcement(
        text=body.text,
        interval_minutes=body.interval_minutes,
        enabled=body.enabled,
        expires_at=body.expires_at,
        server_num=body.server_num,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    await _audit(db, current_user.id, "announcement.create", target_type="announcement", target_id=a.id, detail=a.text)
    await db.commit()
    return AnnouncementOut.model_validate(a)


@app.put("/api/admin/announcements/{announcement_id}", response_model=AnnouncementOut)
async def update_announcement(
    announcement_id: int,
    body: AnnouncementUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    a = (await db.execute(select(Announcement).where(Announcement.id == announcement_id))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Announcement not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(a, field, value)
    await _audit(db, current_user.id, "announcement.update", target_type="announcement", target_id=a.id, detail=a.text)
    await db.commit()
    await db.refresh(a)
    return AnnouncementOut.model_validate(a)


@app.delete("/api/admin/announcements/{announcement_id}", status_code=204)
async def delete_announcement(
    announcement_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    a = (await db.execute(select(Announcement).where(Announcement.id == announcement_id))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Announcement not found")
    await _audit(db, current_user.id, "announcement.delete", target_type="announcement", target_id=a.id, detail=a.text)
    await db.delete(a)
    await db.commit()


@app.post("/api/admin/announcements/{announcement_id}/send-now", response_model=AnnouncementOut)
async def send_announcement_now(
    announcement_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Resets last_sent_at to NULL so the row is immediately "due" on the plugin's next
    poll, without waiting for its interval — a manual "push now" action."""
    a = (await db.execute(select(Announcement).where(Announcement.id == announcement_id))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Announcement not found")
    a.last_sent_at = None
    await _audit(db, current_user.id, "announcement.send_now", target_type="announcement", target_id=a.id, detail=a.text)
    await db.commit()
    await db.refresh(a)
    return AnnouncementOut.model_validate(a)


@app.post("/api/admin/announcements/test-send", response_model=AnnouncementOut, status_code=201)
async def test_send_announcement(
    body: AnnouncementTestSend,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Проверить в игре" — creates a one-off Announcement targeted only at the
    requesting admin's own linked SteamID (current_user.steam_id, set via the in-game
    .register/.login flow), so it broadcasts to nobody else. Auto-expires after 5 minutes
    so it doesn't linger as a stale row, and is excluded from the main
    GET /api/admin/announcements list (see the target_steam_id filter there)."""
    if not current_user.steam_id:
        raise HTTPException(400, "steam_id_not_linked")
    a = Announcement(
        text=body.text,
        interval_minutes=None,
        enabled=True,
        target_steam_id=current_user.steam_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        last_sent_at=None,
        server_num=body.server_num,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    await _audit(db, current_user.id, "announcement.test_send", target_type="announcement", target_id=a.id, detail=a.text)
    await db.commit()
    return AnnouncementOut.model_validate(a)


# ─── Per-server message templates (admin) ──────────────────────────────────────
# Connect/disconnect in-game chat message text, one row per server_num (ServerMessageTemplate
# model), replacing the old global "connect_message_template"/"disconnect_message_template"
# Settings now that the plugin runs on more than one server. Consumed by the plugin via
# GET /api/plugin/message-templates?server_num=N above.

@app.get("/api/admin/message-templates", response_model=ServerMessageTemplateOut)
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


@app.put("/api/admin/message-templates", response_model=ServerMessageTemplateOut)
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

@app.get("/api/admin/server-api-key", response_model=ServerApiKeyOut)
async def get_server_api_key(
    server_num: int = Query(default=1),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(ServerApiKey).where(ServerApiKey.server_num == server_num))
    row = result.scalar_one_or_none()
    return ServerApiKeyOut(api_key=row.api_key if row else "")


@app.put("/api/admin/server-api-key", response_model=ServerApiKeyOut)
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
# Admin-panel counterpart to POST /api/plugin/schedule-restart / cancel-restart above —
# shares the same ScheduledRestart row and _schedule_restart/_cancel_restart helpers so
# the site admin panel and an in-game admin chat command can't get out of sync.

@app.get("/api/admin/servers/{server_num}/restart")
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


@app.post("/api/admin/servers/{server_num}/restart")
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


@app.delete("/api/admin/servers/{server_num}/restart")
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


@app.get("/api/admin/servers/{server_num}/daily-restart")
async def get_daily_restart(
    server_num: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(ScheduledRestart).where(ScheduledRestart.server_num == server_num))
    row = result.scalar_one_or_none()
    return {"daily_restart_time": row.daily_restart_time if row else None}


@app.post("/api/admin/servers/{server_num}/daily-restart")
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


@app.delete("/api/admin/servers/{server_num}/daily-restart")
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


# ─── Player bans (admin) ───────────────────────────────────────────────────────
# Admin-panel counterpart to the POST /api/plugin/ban / unban / GET .../due-unbans trio
# above — see models.Ban's docstring for the full active/unban_at/unbanned_at lifecycle.

@app.get("/api/admin/bans")
async def list_bans(
    status: str = Query(default="active"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Bans across every server, most recent first. status controls which ones:
    "active" (default — preserves the original behavior of this endpoint, since existing
    callers assume active-only) = unbanned_at IS NULL; "resolved" = unbanned_at IS NOT
    NULL (already lifted, read-only history for the admin UI); "all" = no filter. Any
    other value falls back to "active". unban_at null means permanent; a timestamp is the
    scheduled expiry, for the admin UI to compute/display a "time remaining" countdown.
    unbanned_at (null unless the ban has actually been lifted) is always included so the
    "resolved" view can show when it was lifted. server_name is included as a convenience
    (same server_num -> real-name lookup used by GET /api/clans) so the admin page doesn't
    need a second round-trip just to label the server column."""
    server_names = await _get_server_names(db)
    query = select(Ban).order_by(Ban.banned_at.desc())
    if status == "resolved":
        query = query.where(Ban.unbanned_at.is_not(None))
    elif status == "all":
        pass
    else:
        query = query.where(Ban.unbanned_at.is_(None))
    result = await db.execute(query)
    bans = result.scalars().all()
    return {
        "bans": [
            {
                "id": b.id,
                "server_num": b.server_num,
                "server_name": server_names.get(b.server_num) or f"Сервер {b.server_num}",
                "steam_id": b.steam_id,
                "character_name": b.character_name,
                "admin_name": b.admin_name,
                "reason": b.reason,
                "banned_at": _fmt_dt_z(b.banned_at),
                "unban_at": _fmt_dt_z(b.unban_at),
                "unbanned_at": _fmt_dt_z(b.unbanned_at),
            }
            for b in bans
        ]
    }


@app.post("/api/admin/bans/{ban_id}/unban")
async def unban_admin(
    ban_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Brings unban_at forward to "now" (regardless of its previous value) rather than
    setting unbanned_at directly — that only happens once the plugin actually confirms the
    real in-game unban via POST /api/plugin/unban or the next GET /api/plugin/due-unbans
    poll consumes this row (within ~60s). 404 if ban_id doesn't exist or is already
    resolved (unbanned_at set)."""
    result = await db.execute(select(Ban).where(Ban.id == ban_id, Ban.unbanned_at.is_(None)))
    ban = result.scalar_one_or_none()
    if ban is None:
        raise HTTPException(status_code=404, detail="Ban not found")
    _force_unban(ban)
    await _audit(db, current_user.id, "ban.unban", target_type="ban", target_id=ban.id, detail=ban.steam_id)
    await db.commit()
    return {"success": True}


# ─── Ban appeals ────────────────────────────────────────────────────────────────
# A banned player is blocked from the GAME SERVER but their SITE account (if any) is
# unaffected, so appealing must work WITHOUT a site login — just the SteamID they can find
# via the Steam client or the ban announcement they saw in-game. See models.BanAppeal's
# docstring for the full lifecycle.

@app.post("/api/appeals")
@limiter.limit("3/hour")
async def submit_ban_appeal(request: Request, body: BanAppealCreate, db: AsyncSession = Depends(get_db)):
    """Public, unauthenticated. Looks up the currently-active Ban for body.steam_id (any
    server_num — most recent if somehow more than one) so random non-banned visitors can't
    spam this; 400 "no_active_ban" if none found. 400 "already_appealed" if a pending
    appeal already exists for that same ban — they wait for a response instead of stacking
    appeals. character_name is taken from the Ban row itself (authoritative) rather than
    the request body, once found."""
    result = await db.execute(
        select(Ban)
        .where(Ban.steam_id == body.steam_id, Ban.unbanned_at.is_(None))
        .order_by(Ban.banned_at.desc())
    )
    ban = result.scalars().first()
    if ban is None:
        raise HTTPException(status_code=400, detail="no_active_ban")

    existing = await db.execute(
        select(BanAppeal).where(BanAppeal.ban_id == ban.id, BanAppeal.status == "pending")
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="already_appealed")

    db.add(BanAppeal(
        ban_id=ban.id,
        steam_id=body.steam_id,
        character_name=ban.character_name or body.character_name,
        message=body.message,
        status="pending",
        created_at=datetime.utcnow(),
    ))
    await db.commit()
    return {"success": True}


@app.get("/api/admin/appeals")
async def list_ban_appeals(
    status: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Most-recent-first; status query param optionally filters to "pending"/"approved"/
    "rejected" (default: all). ban_reason/ban_admin_name are joined through ban_id for
    admin context — null if the underlying Ban row is somehow gone."""
    q = select(BanAppeal).order_by(BanAppeal.created_at.desc())
    if status:
        q = q.where(BanAppeal.status == status)
    result = await db.execute(q)
    appeals = result.scalars().all()

    ban_ids = {a.ban_id for a in appeals if a.ban_id is not None}
    bans_by_id: dict[int, Ban] = {}
    if ban_ids:
        ban_result = await db.execute(select(Ban).where(Ban.id.in_(ban_ids)))
        bans_by_id = {b.id: b for b in ban_result.scalars().all()}

    return {
        "appeals": [
            {
                "id": a.id,
                "steam_id": a.steam_id,
                "character_name": a.character_name,
                "message": a.message,
                "status": a.status,
                "admin_response": a.admin_response,
                "admin_name": a.admin_name,
                "created_at": _fmt_dt_z(a.created_at),
                "resolved_at": _fmt_dt_z(a.resolved_at),
                "ban_reason": bans_by_id[a.ban_id].reason if a.ban_id in bans_by_id else None,
                "ban_admin_name": bans_by_id[a.ban_id].admin_name if a.ban_id in bans_by_id else None,
            }
            for a in appeals
        ]
    }


@app.post("/api/admin/appeals/{appeal_id}/resolve")
async def resolve_ban_appeal(
    appeal_id: int,
    body: AppealResolveIn,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Approving ALSO lifts the underlying ban the exact same way the existing bans.html
    "Разбанить" button does (see _force_unban above) — sets Ban.unban_at to "now", never
    unbanned_at directly. 404 if the appeal doesn't exist or was already resolved."""
    result = await db.execute(
        select(BanAppeal).where(BanAppeal.id == appeal_id, BanAppeal.status == "pending")
    )
    appeal = result.scalar_one_or_none()
    if appeal is None:
        raise HTTPException(status_code=404, detail="Appeal not found")

    appeal.status = "approved" if body.approve else "rejected"
    appeal.admin_response = body.admin_response
    appeal.admin_name = current_user.username
    appeal.resolved_at = datetime.utcnow()

    if body.approve and appeal.ban_id is not None:
        ban_result = await db.execute(select(Ban).where(Ban.id == appeal.ban_id, Ban.unbanned_at.is_(None)))
        ban = ban_result.scalar_one_or_none()
        if ban is not None:
            _force_unban(ban)

    await _audit(db, current_user.id, "appeal.resolve", target_type="ban_appeal", target_id=appeal.id, detail=appeal.status)
    await db.commit()
    return {"success": True}


# ─── Unified moderation log ─────────────────────────────────────────────────────
# One chronological feed of every moderation action. Ban/Warning already capture
# ban/unban/warn with everything needed, so they're merged in here rather than
# re-logged; ModerationLogEntry (below) only stores the action types those two tables
# don't cover.

@app.get("/api/admin/moderation-log")
async def get_moderation_log(
    limit: int = Query(default=100, le=500),
    server_num: Optional[int] = Query(default=None),
    steam_id: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Merges three sources into one common shape, sorts by timestamp descending in
    Python, then applies limit — result sets here are small and admin-only/infrequent, so
    a UNION SQL query would save little. Ban rows become one "ban" entry each (using
    banned_at) plus, if the ban has actually been lifted (unbanned_at IS NOT NULL), a
    second "unban" entry (using unbanned_at) — there's no site-side tracking of WHO
    unbanned distinctly from the ban's original admin, so admin_name is reused rather than
    left null. Warning rows become "warn" entries (reason -> details). ModerationLogEntry
    rows (kick/mute/unmute/restart_scheduled/restart_executed, written by
    POST /api/plugin/log-action) pass through as-is. steam_id, when given, narrows the feed
    to just that player (matched against each source's own steam-id column — target_steam_id
    in the merged output) — layered on top of the server_num filter, e.g. from bans.html's
    "История" link into this page for one banned player."""
    ban_q = select(Ban)
    warn_q = select(Warning)
    log_q = select(ModerationLogEntry)
    if server_num is not None:
        ban_q = ban_q.where(Ban.server_num == server_num)
        warn_q = warn_q.where(Warning.server_num == server_num)
        log_q = log_q.where(ModerationLogEntry.server_num == server_num)
    if steam_id is not None:
        ban_q = ban_q.where(Ban.steam_id == steam_id)
        warn_q = warn_q.where(Warning.steam_id == steam_id)
        log_q = log_q.where(ModerationLogEntry.target_steam_id == steam_id)

    bans = (await db.execute(ban_q)).scalars().all()
    warnings = (await db.execute(warn_q)).scalars().all()
    log_entries = (await db.execute(log_q)).scalars().all()

    entries = []
    for b in bans:
        entries.append({
            "action": "ban",
            "server_num": b.server_num,
            "admin_name": b.admin_name,
            "target_name": b.character_name,
            "target_steam_id": b.steam_id,
            "details": b.reason,
            "created_at": b.banned_at,
        })
        if b.unbanned_at is not None:
            entries.append({
                "action": "unban",
                "server_num": b.server_num,
                "admin_name": b.admin_name,
                "target_name": b.character_name,
                "target_steam_id": b.steam_id,
                "details": None,
                "created_at": b.unbanned_at,
            })
    for w in warnings:
        entries.append({
            "action": "warn",
            "server_num": w.server_num,
            "admin_name": w.admin_name,
            "target_name": w.character_name,
            "target_steam_id": w.steam_id,
            "details": w.reason,
            "created_at": w.created_at,
        })
    for e in log_entries:
        entries.append({
            "action": e.action,
            "server_num": e.server_num,
            "admin_name": e.admin_name,
            "target_name": e.target_name,
            "target_steam_id": e.target_steam_id,
            "details": e.details,
            "created_at": e.created_at,
        })

    entries.sort(key=lambda e: e["created_at"], reverse=True)
    entries = entries[:limit]
    return {
        "log": [{**e, "created_at": _fmt_dt_z(e["created_at"])} for e in entries]
    }


# ─── Who's online ─────────────────────────────────────────────────────────────
_visitor_data: dict[str, dict] = {}  # visitor_id -> {ts, first_ts, db_ts, page, username, is_authed, is_bot}

_BOT_UA = re.compile(
    r'bot|crawler|spider|slurp|yandex|baidu|bing|google|duckduck|semrush|ahrefs'
    r'|mj12|dataprovider|proximic|gigabot|dotbot|rogerbot|facebookexternalhit'
    r'|twitterbot|discordbot|telegrambot|whatsapp|slackbot|linkedinbot|applebot'
    r'|pingdom|uptimerobot|checkly|chrome-lighthouse|headlesschrome|phantomjs',
    re.I,
)
_MOBILE_UA = re.compile(
    r'Mobile|Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|webOS|Windows Phone',
    re.I,
)
_peak_today: dict = {"count": 0, "at_ts": 0.0, "date": ""}
_activity_history: list[dict] = []   # [{ts, count}] каждые 5 мин, макс 24ч
_sse_clients: set = set()            # asyncio.Queue для каждого SSE-клиента
_explicit_logouts: dict[str, float] = {}  # username -> logout timestamp
_ingame_players: dict[str, float] = {}   # player_name_lower -> last_seen_ts
_GUEST_TTL = 120   # 2 minutes
_INGAME_TTL = 900  # 15 minutes (≈3 monitor polls)

_PAGE_LABELS = {
    "/": "Главная", "/index.html": "Главная",
    "/servers.html": "Серверы", "/leaderboard.html": "Игроки",
    "/clans.html": "Кланы", "/bans.html": "Баны",
    "/map.html": "Карта", "/faq.html": "FAQ",
    "/profile.html": "Профиль", "/login.html": "Вход",
}


async def _sse_broadcast(payload: str) -> None:
    dead = set()
    for q in _sse_clients:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.add(q)
    _sse_clients.difference_update(dead)


class OnlinePingBody(BaseModel):
    visitor_id: str
    is_authed: bool = False
    username: Optional[str] = None
    page: str = "Сайт"


@app.post("/api/online/ping", status_code=204)
@limiter.limit("20/minute")
async def online_ping(
    request: Request,
    body: OnlinePingBody,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    # Identity comes from the session (cookie/bearer token), never from the client-
    # asserted body.is_authed/body.username — those are self-reported and let anyone
    # spoof any username into the public "who's online" widget and write to that
    # user's last_active_at with no auth at all.
    is_authed = current_user is not None
    username = current_user.username if current_user else None
    cutoff = time.time() - _GUEST_TTL
    for vid in list(_visitor_data):
        if _visitor_data[vid]["ts"] < cutoff:
            del _visitor_data[vid]
    ua = request.headers.get("user-agent", "")
    is_bot = bool(_BOT_UA.search(ua))
    is_mobile = bool(_MOBILE_UA.search(ua)) and not is_bot
    if len(body.visitor_id) <= 64:
        now_ts = time.time()
        existing = _visitor_data.get(body.visitor_id, {})
        _visitor_data[body.visitor_id] = {
            "ts": now_ts,
            "first_ts": existing.get("first_ts", now_ts),
            "db_ts": existing.get("db_ts", 0),
            "page": (body.page or "Сайт")[:64],
            "username": username,
            "is_authed": is_authed,
            "is_bot": is_bot,
            "device": "mobile" if is_mobile else "desktop",
        }
    # Keep last_active_at fresh so the user appears in the online widget immediately.
    if is_authed and username:
        db_ts = _visitor_data.get(body.visitor_id, {}).get("db_ts", 0)
        if time.time() - db_ts > 55:
            current_user.last_active_at = datetime.now(timezone.utc)
            await db.commit()
            _visitor_data[body.visitor_id]["db_ts"] = time.time()
    asyncio.create_task(_sse_broadcast("update"))
    return Response(status_code=204)


@app.get("/api/online")
async def online_status(db: AsyncSession = Depends(get_db)):
    now_ts = time.time()
    cutoff_ts = now_ts - _GUEST_TTL

    # Purge stale explicit logouts (keep 5 min)
    for u in list(_explicit_logouts):
        if _explicit_logouts[u] < now_ts - 300:
            del _explicit_logouts[u]

    user_pages: dict[str, str] = {}
    user_since: dict[str, float] = {}
    guests = 0
    bots = 0
    for d in _visitor_data.values():
        if d["ts"] < cutoff_ts:
            continue
        if d.get("is_bot"):
            bots += 1
        elif d.get("is_authed") and d.get("username"):
            uname = d["username"]
            user_pages[uname] = d.get("page", "")
            user_since[uname] = d.get("first_ts", d["ts"])
        else:
            guests += 1

    ingame_cutoff = now_ts - _INGAME_TTL
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).replace(tzinfo=None)
    result = await db.execute(
        select(User.username, User.avatar_url, User.role, User.created_at)
        .where(User.is_active == True, User.last_active_at != None, User.last_active_at >= cutoff)
        .order_by(User.last_active_at.desc())
        .limit(20)
    )
    users = [
        {"username": r.username, "avatar_url": r.avatar_url, "role": r.role,
         "page": user_pages.get(r.username, ""),
         "in_game": _ingame_players.get(r.username.lower(), 0) > ingame_cutoff,
         "since": user_since.get(r.username, now_ts),
         "device": next((d.get("device","desktop") for d in _visitor_data.values() if d.get("username")==r.username and d.get("is_authed")), "desktop"),
         "registered_at": _fmt_dt(r.created_at)}
        for r in result.all()
        if r.username not in _explicit_logouts
    ]

    # Track peak online today (reset at UTC midnight)
    total = len(users) + guests
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _peak_today.get("date") != today:
        _peak_today.update({"count": 0, "at_ts": 0.0, "date": today})
    if total > _peak_today["count"]:
        _peak_today["count"] = total
        _peak_today["at_ts"] = now_ts

    # Sample activity history every 5 min
    if not _activity_history or now_ts - _activity_history[-1]["ts"] >= 300:
        _activity_history.append({"ts": now_ts, "count": total})
        cutoff_hist = now_ts - 86400
        while _activity_history and _activity_history[0]["ts"] < cutoff_hist:
            _activity_history.pop(0)

    page_counts: dict[str, int] = {}
    for u in users:
        if u["page"]:
            page_counts[u["page"]] = page_counts.get(u["page"], 0) + 1
    return {
        "users": users, "guests": guests, "bots": bots, "total": total, "page_counts": page_counts,
        "peak_today": {"count": _peak_today["count"], "at_ts": _peak_today["at_ts"]},
        "history": list(_activity_history),
    }


@app.get("/api/online/stream")
async def online_stream():
    import asyncio as _aio
    queue: _aio.Queue = _aio.Queue(maxsize=20)
    _sse_clients.add(queue)

    async def generate():
        try:
            while True:
                try:
                    await _aio.wait_for(queue.get(), timeout=25)
                    yield "data: update\n\n"
                except _aio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_clients.discard(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/auth/accept-rules", response_model=UserOut)
async def accept_rules(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.rules_accepted_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@app.post("/api/auth/change-password")
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    body: ChangePasswordBody,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    old = body.old_password.strip()
    new = body.new_password
    if not old:
        raise HTTPException(400, "Заполните все поля")
    if not verify_password(old, current_user.hashed_password):
        raise HTTPException(400, "Неверный текущий пароль")
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.hashed_password = get_password_hash(new)
    # Invalidate every token issued before now (e.g. a stolen/leaked one on another
    # device) — the exact moment a user expects to be safe again. Then immediately
    # issue a fresh token for THIS session so the browser that just changed the
    # password isn't itself logged out.
    #
    # The revoke cutoff is backdated by 1s on purpose: create_access_token's `iat` is
    # a JWT NumericDate (whole seconds), but this timestamp has microsecond precision.
    # A token minted in the same wall-clock second as an un-backdated `now()` could
    # get an `iat` that's *earlier* than this cutoff by comparison
    # (get_current_user checks `iat_datetime < revoke_before`), instantly revoking the
    # very token meant to keep this session alive. Any token that actually predates
    # this request — the real threat — is still comfortably older than "now - 1s".
    now_utc = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await db.execute(text("UPDATE users SET revoke_before = :ts WHERE id = :uid"), {"ts": now_utc, "uid": current_user.id})
    await db.commit()
    token = create_access_token({"sub": str(current_user.id)})
    _set_auth_cookie(response, token)
    return {"ok": True, "access_token": token}


@app.post("/api/auth/change-email")
@limiter.limit("5/minute")
async def change_email(
    request: Request,
    body: ChangeEmailBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.password, current_user.hashed_password):
        raise HTTPException(400, "Неверный пароль")
    new_email = body.new_email.strip().lower()
    existing = await db.execute(
        select(User.id).where(User.email == new_email, User.id != current_user.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Этот email уже используется")
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.email = new_email
    await db.commit()
    return {"ok": True, "email": new_email}


class TotpCodeBody(BaseModel):
    code: str


@app.get("/api/auth/2fa/setup")
async def totp_setup(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.totp_enabled:
        raise HTTPException(400, "2FA уже включена")
    import pyotp
    secret = pyotp.random_base32()
    _totp_pending[current_user.id] = secret
    uri = pyotp.totp.TOTP(secret).provisioning_uri(current_user.email, issuer_name="V Rising")
    return {"secret": secret, "otpauth_uri": uri}


@app.post("/api/auth/2fa/enable")
async def totp_enable(
    body: TotpCodeBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import pyotp
    secret = _totp_pending.get(current_user.id)
    if not secret:
        raise HTTPException(400, "Сначала вызовите /api/auth/2fa/setup")
    if not pyotp.TOTP(secret).verify(body.code):
        raise HTTPException(400, "Неверный код")
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.totp_secret = secret
    user.totp_enabled = True
    await db.commit()
    _totp_pending.pop(current_user.id, None)
    return {"ok": True}


@app.post("/api/auth/2fa/disable")
async def totp_disable(
    body: TotpCodeBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import pyotp
    if not current_user.totp_enabled:
        raise HTTPException(400, "2FA не включена")
    if not pyotp.TOTP(current_user.totp_secret).verify(body.code, valid_window=1):
        raise HTTPException(400, "Неверный код")
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.totp_enabled = False
    user.totp_secret = None
    await db.commit()
    return {"ok": True}


@app.post("/api/auth/forgot-password")
@limiter.limit("3/minute;10/hour")
async def forgot_password(request: Request, body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email, User.is_active == True))
    user = result.scalar_one_or_none()
    if user:
        # Delete old unused tokens for this user
        await db.execute(
            delete(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used == False)
        )
        token = uuid.uuid4().hex
        db.add(PasswordReset(
            user_id=user.id,
            token=token,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        ))
        await db.commit()
        # Send reset email
        base_url = str(request.base_url).rstrip("/")
        reset_url = f"{base_url}/reset-password.html?token={token}"
        email_sent = await _send_reset_email(user.email, reset_url)
        if email_sent:
            return {"message": "Ссылка для сброса пароля отправлена на ваш email."}
    # Always return success to prevent email enumeration
    return {"message": "Если аккаунт с таким email существует, запрос создан. Обратитесь к администратору."}


@app.get("/api/auth/reset-password/{token}")
async def validate_reset_token(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PasswordReset).where(
            PasswordReset.token == token,
            PasswordReset.used == False,
            PasswordReset.expires_at > datetime.now(timezone.utc),
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(400, "Ссылка недействительна или истекла")
    return {"valid": True}


@app.post("/api/auth/reset-password/{token}")
@limiter.limit("5/minute")
async def do_reset_password(request: Request, token: str, body: ResetPasswordBody, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PasswordReset).where(
            PasswordReset.token == token,
            PasswordReset.used == False,
            PasswordReset.expires_at > datetime.now(timezone.utc),
        )
    )
    reset = result.scalar_one_or_none()
    if not reset:
        raise HTTPException(400, "Ссылка недействительна или истекла")
    user_result = await db.execute(select(User).where(User.id == reset.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(400, "Пользователь не найден")
    user.hashed_password = get_password_hash(body.new_password)
    reset.used = True
    # Same reasoning as change-password: a reset means the old password (and any
    # session token issued under it) may be compromised — invalidate everything
    # issued before now. No session to re-issue here since this flow is unauthenticated.
    now_utc = datetime.now(timezone.utc).isoformat()
    await db.execute(text("UPDATE users SET revoke_before = :ts WHERE id = :uid"), {"ts": now_utc, "uid": user.id})
    await db.commit()
    return {"message": "Пароль успешно изменён"}


@app.get("/api/admin/password-resets")
async def list_password_resets(_: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PasswordReset, User.username, User.email)
        .join(User, PasswordReset.user_id == User.id)
        .where(PasswordReset.used == False, PasswordReset.expires_at > datetime.now(timezone.utc))
        .order_by(PasswordReset.created_at.desc())
    )
    return [
        {
            "token": row[0].token,
            "username": row[1],
            "email": row[2],
            "created_at": row[0].created_at.isoformat(),
            "expires_at": row[0].expires_at.isoformat(),
        }
        for row in result.all()
    ]


@app.post("/api/auth/avatar")
@limiter.limit("10/minute")
async def upload_avatar(
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
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 5 MB)")
    fname = f"avatar_{current_user.id}_{uuid.uuid4().hex[:10]}{ext}"
    (UPLOAD_DIR / fname).write_bytes(content)
    # remove old avatar file
    old = current_user.avatar_url or ""
    if old:
        old_name = old.rsplit("/", 1)[-1]
        old_path = UPLOAD_DIR / old_name
        if old_name.startswith("avatar_") and old_path.exists():
            old_path.unlink(missing_ok=True)
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.avatar_url = f"/api/uploads/{fname}"
    await db.commit()
    return {"avatar_url": user.avatar_url}


# ─── Profile cover ───────────────────────────────────────────────────────────

@app.post("/api/profile/cover")
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

@app.put("/api/profile/bio")
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


# ─── Monitor ────────────────────────────────────────────────────────────────

async def _track_players(db: AsyncSession, players: list, server_num: int):
    if not players:
        return
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    for p in players:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        _ingame_players[name.lower()] = now_ts  # cache for online widget
        cur_dur = int(p.get("duration", 0))
        result = await db.execute(
            select(PlayerRecord).where(
                PlayerRecord.server_num == server_num,
                PlayerRecord.player_name == name,
            )
        )
        rec = result.scalar_one_or_none()
        if rec is None:
            db.add(PlayerRecord(
                server_num=server_num,
                player_name=name,
                total_seconds=cur_dur,
                last_seen=now,
                last_duration=cur_dur,
                session_count=1,
            ))
        else:
            if cur_dur >= rec.last_duration:
                rec.total_seconds += cur_dur - rec.last_duration
            else:
                rec.total_seconds += cur_dur
                rec.session_count += 1
            rec.last_duration = cur_dur
            rec.last_seen = now
    await db.commit()


_last_snapshot: dict[int, float] = {}
SNAPSHOT_INTERVAL = 300  # 5 minutes

# ─── Server status SSE broadcast & in-memory cache ───────────────────────────
_sse_queues: list[asyncio.Queue] = []
_status_cache: dict[int, dict] = {}
_status_cache_ts: dict[int, float] = {}
STATUS_CACHE_TTL = 28  # seconds


def _broadcast_status(data: dict) -> None:
    """Put server status update into all active SSE client queues."""
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_queues.remove(q)
        except ValueError:
            pass


async def _upsert_setting(db: AsyncSession, key: str, value: str):
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
        setting.updated_at = datetime.now(timezone.utc)
    else:
        db.add(Setting(key=key, value=value))


async def _save_snapshot(db: AsyncSession, data: dict, server_num: int):
    now_ts = time.time()
    if now_ts - _last_snapshot.get(server_num, 0) < SNAPSHOT_INTERVAL:
        return
    _last_snapshot[server_num] = now_ts
    players = data.get("players", 0)
    snap = ServerSnapshot(
        server_num=server_num,
        recorded_at=datetime.now(timezone.utc),
        online=data.get("online", False),
        players=players,
        max_players=data.get("max_players", 0),
        latency_ms=data.get("latency_ms"),
        map_name=data.get("map"),
    )
    db.add(snap)

    peak_key = f"peak_alltime_{server_num}"
    result = await db.execute(select(Setting).where(Setting.key == peak_key))
    peak_setting = result.scalar_one_or_none()
    current_peak = int(peak_setting.value) if peak_setting and peak_setting.value.isdigit() else 0
    if players > current_peak:
        await _upsert_setting(db, peak_key, str(players))
        await _upsert_setting(db, f"{peak_key}_date", datetime.now(timezone.utc).isoformat())

    await db.commit()
    # prune old snapshots (keep 8 days)
    cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff -= timedelta(days=8)
    await db.execute(
        delete(ServerSnapshot).where(
            ServerSnapshot.server_num == server_num,
            ServerSnapshot.recorded_at < cutoff,
        )
    )
    await db.commit()


@app.get("/api/monitor/status")
async def server_status(db: AsyncSession = Depends(get_db)):
    # Serve from cache if fresh
    cached = _status_cache.get(1)
    cached_ts = _status_cache_ts.get(1, 0)
    if cached and (time.time() - cached_ts) < STATUS_CACHE_TTL:
        return cached
    result = await db.execute(
        select(Setting).where(Setting.key.in_(["server_ip", "server_port", "server_name", "server_game_port", "server_connect_ip"]))
    )
    cfg = {s.key: s.value for s in result.scalars().all()}
    ip = cfg.get("server_ip", "127.0.0.1")
    port = int(cfg.get("server_port", "27016"))
    admin_name = cfg.get("server_name", "").strip()
    game_port_str = cfg.get("server_game_port", "").strip()
    connect_ip = cfg.get("server_connect_ip", "").strip() or ip
    data = await get_server_status(ip, port)
    if admin_name:
        data = {**data, "name": admin_name}
    elif not data.get("name") or data.get("name") == "Unknown":
        data = {**data, "name": "V Rising Server"}
    data = {**data, "ip": connect_ip, "game_port": int(game_port_str) if game_port_str.isdigit() else None}
    await _track_players(db, data.get("players_list", []), 1)
    await _save_snapshot(db, data, 1)
    _status_cache[1] = data
    _status_cache_ts[1] = time.time()
    return data


@app.get("/api/monitor/history")
async def server_history(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Setting).where(Setting.key.in_(["server_ip", "server_port"]))
    )
    cfg = {s.key: s.value for s in result.scalars().all()}
    ip = cfg.get("server_ip", "127.0.0.1")
    port = int(cfg.get("server_port", "27016"))
    return get_history(ip, port)


@app.get("/api/monitor/history2")
async def server_history2(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Setting).where(Setting.key.in_(["server2_ip", "server2_port"]))
    )
    cfg = {s.key: s.value for s in result.scalars().all()}
    ip = cfg.get("server2_ip", "").strip()
    if not ip:
        return []
    port_str = cfg.get("server2_port", "27016")
    port = int(port_str) if port_str.isdigit() else 27016
    return get_history(ip, port)


@app.get("/api/monitor/snapshots")
async def get_snapshots(server: int = Query(1), days: int = Query(default=7, ge=1, le=90), db: AsyncSession = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(ServerSnapshot)
        .where(ServerSnapshot.server_num == server, ServerSnapshot.recorded_at >= cutoff)
        .order_by(ServerSnapshot.recorded_at.asc())
    )
    snaps = result.scalars().all()
    return [{"ts": int(_utc_ts(s.recorded_at)), "players": s.players, "online": s.online, "latency_ms": s.latency_ms} for s in snaps]


@app.get("/api/monitor/stats")
async def get_monitor_stats(server: int = Query(1), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    day_ago  = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    res_week = await db.execute(
        select(ServerSnapshot)
        .where(ServerSnapshot.server_num == server, ServerSnapshot.recorded_at >= week_ago)
    )
    snaps_week = res_week.scalars().all()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt

    day_ago_naive = _naive(day_ago)
    res_day = [s for s in snaps_week if _naive(s.recorded_at) >= day_ago_naive]

    def uptime_pct(snaps):
        if not snaps:
            return None
        return round(sum(1 for s in snaps if s.online) / len(snaps) * 100, 1)

    peak_7d = max((s.players for s in snaps_week), default=0)
    peak_24h = max((s.players for s in res_day), default=0)

    # hourly heatmap: avg players per hour-of-day over last 7 days (local tz)
    tz_res = await db.execute(select(Setting).where(Setting.key == "timezone"))
    tz_setting = tz_res.scalar_one_or_none()
    tz_name = tz_setting.value if tz_setting else None
    try:
        _tz = ZoneInfo(tz_name or "Europe/Moscow")
    except Exception:
        _tz = ZoneInfo("Europe/Moscow")

    def _local_hour(dt):
        return dt.replace(tzinfo=timezone.utc).astimezone(_tz).hour

    buckets: dict[int, list[int]] = {h: [] for h in range(24)}
    for s in snaps_week:
        buckets[_local_hour(s.recorded_at)].append(s.players)
    heatmap = [round(sum(v) / len(v), 1) if v else 0 for _, v in sorted(buckets.items())]

    peak_result = await db.execute(
        select(Setting).where(Setting.key.in_([f"peak_alltime_{server}", f"peak_alltime_{server}_date"]))
    )
    peak_cfg = {s.key: s.value for s in peak_result.scalars().all()}
    peak_alltime = peak_cfg.get(f"peak_alltime_{server}")
    peak_alltime_date = peak_cfg.get(f"peak_alltime_{server}_date")

    return {
        "uptime_24h": uptime_pct(res_day),
        "uptime_7d":  uptime_pct(snaps_week),
        "peak_24h":   peak_24h,
        "peak_7d":    peak_7d,
        "peak_alltime":      int(peak_alltime) if peak_alltime and peak_alltime.isdigit() else max(peak_7d, 0),
        "peak_alltime_date": peak_alltime_date,
        "heatmap":    heatmap,
    }


@app.get("/api/monitor/status/stream")
async def monitor_status_stream():
    """SSE endpoint — pushes server status updates to connected clients."""
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    _sse_queues.append(q)

    async def generator():
        try:
            yield "data: {\"ping\": true}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _sse_queues.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/monitor/status2")
async def server_status2(db: AsyncSession = Depends(get_db)):
    # Serve from cache if fresh
    cached = _status_cache.get(2)
    cached_ts = _status_cache_ts.get(2, 0)
    if cached and (time.time() - cached_ts) < STATUS_CACHE_TTL:
        return cached
    result = await db.execute(
        select(Setting).where(Setting.key.in_(["server2_ip", "server2_port", "server2_name", "server2_game_port", "server2_connect_ip"]))
    )
    cfg = {s.key: s.value for s in result.scalars().all()}
    ip = cfg.get("server2_ip", "").strip()
    admin_name = cfg.get("server2_name", "").strip()
    if not ip:
        return {"enabled": False, "online": False, "name": admin_name or "Server 2",
                "players": 0, "max_players": 0, "players_list": []}
    port_str = cfg.get("server2_port", "27016")
    port = int(port_str) if port_str.isdigit() else 27016
    game_port_str = cfg.get("server2_game_port", "").strip()
    connect_ip = cfg.get("server2_connect_ip", "").strip() or ip
    data = await get_server_status(ip, port)
    if admin_name:
        data = {**data, "name": admin_name}
    elif not data.get("name") or data.get("name") == "Unknown":
        data = {**data, "name": "Server 2"}
    data = {**data, "ip": connect_ip, "game_port": int(game_port_str) if game_port_str.isdigit() else None}
    await _track_players(db, data.get("players_list", []), 2)
    await _save_snapshot(db, data, 2)
    _status_cache[2] = {"enabled": True, **data}
    _status_cache_ts[2] = time.time()
    return {"enabled": True, **data}


# ─── News (public) ──────────────────────────────────────────────────────────

@app.get("/api/news/tags")
async def list_tags(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(News.tags).where(News.published == True, News.tags != None, News.tags != "")
    )
    all_tags: set[str] = set()
    for (tags_str,) in result.all():
        if tags_str:
            for t in tags_str.split(","):
                t = t.strip()
                if t:
                    all_tags.add(t)
    return sorted(all_tags)


@app.get("/api/news", response_model=PaginatedNews)
async def list_news(
    page: int = Query(1, ge=1),
    per_page: int = Query(5, ge=1, le=50),
    tag: str = Query(None),
    search: str = Query(None, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    base_filter = News.published == True
    if tag:
        base_filter = base_filter & News.tags.contains(tag)
    if search:
        term = f"%{search}%"
        base_filter = base_filter & (News.title.ilike(term) | News.summary.ilike(term))

    total_result = await db.execute(
        select(func.count()).select_from(News).where(base_filter)
    )
    total = total_result.scalar_one()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    result = await db.execute(
        select(News)
        .where(base_filter)
        .order_by(News.pinned.desc(), News.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    items = result.scalars().all()
    news_ids = [n.id for n in items]
    counts: dict[int, int] = {}
    if news_ids:
        cnt_result = await db.execute(
            select(Comment.news_id, func.count(Comment.id))
            .where(Comment.news_id.in_(news_ids))
            .group_by(Comment.news_id)
        )
        counts = {row[0]: row[1] for row in cnt_result.all()}
    out = []
    for n in items:
        d = NewsListOut.model_validate(n)
        d.comment_count = counts.get(n.id, 0)
        out.append(d)
    return PaginatedNews(
        items=out,
        total=total,
        page=page,
        pages=pages,
    )


@app.get("/api/news/{slug}", response_model=NewsOut)
async def get_news(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(News).where(News.slug == slug, News.published == True))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    # Deduplicate views: count only once per IP per 24h using page_views table
    ip = request.client.host if request.client else ""
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else None
    view_key = f"/news/{news.id}"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = await db.execute(
        select(PageView.id).where(
            PageView.path == view_key,
            PageView.ip_hash == ip_hash,
            PageView.created_at >= cutoff,
        ).limit(1)
    )
    news_id = news.id
    if recent.scalar_one_or_none() is None:
        news.views = (news.views or 0) + 1
        db.add(PageView(path=view_key, ip_hash=ip_hash))
    await db.commit()
    result2 = await db.execute(
        select(News).options(selectinload(News.author)).where(News.id == news_id)
    )
    news = result2.scalar_one()
    return NewsOut.model_validate(news)


# ─── Reactions ───────────────────────────────────────────────────────────────

_ALLOWED_EMOJIS = {"fire", "heart", "thumbs_up", "wow"}


@app.get("/api/news/{slug}/reactions")
async def get_reactions(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
):
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(404, "Not found")
    counts_res = await db.execute(
        select(Reaction.emoji, func.count(Reaction.id))
        .where(Reaction.news_id == news_id)
        .group_by(Reaction.emoji)
    )
    counts = {row[0]: row[1] for row in counts_res.all()}
    user_reactions: list[str] = []
    # Try Bearer header first, then cookie
    token: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                uid = int(payload.get("sub", 0))
                if uid:
                    ur_res = await db.execute(
                        select(Reaction.emoji).where(Reaction.news_id == news_id, Reaction.user_id == uid)
                    )
                    user_reactions = [r[0] for r in ur_res.all()]
        except Exception:
            pass
    return {"counts": counts, "user_reactions": user_reactions}


@app.post("/api/news/{slug}/react")
@limiter.limit("30/minute")
async def toggle_reaction(
    request: Request,
    slug: str,
    body: ReactBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    emoji = body.emoji
    if emoji not in _ALLOWED_EMOJIS:
        raise HTTPException(400, "Invalid emoji")
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(404, "Not found")
    existing_res = await db.execute(
        select(Reaction).where(
            Reaction.news_id == news_id,
            Reaction.user_id == current_user.id,
            Reaction.emoji == emoji,
        )
    )
    existing = existing_res.scalar_one_or_none()
    if existing:
        await db.delete(existing)
    else:
        db.add(Reaction(news_id=news_id, user_id=current_user.id, emoji=emoji))
    await db.commit()
    counts_res = await db.execute(
        select(Reaction.emoji, func.count(Reaction.id))
        .where(Reaction.news_id == news_id)
        .group_by(Reaction.emoji)
    )
    counts = {row[0]: row[1] for row in counts_res.all()}
    ur_res = await db.execute(
        select(Reaction.emoji).where(Reaction.news_id == news_id, Reaction.user_id == current_user.id)
    )
    user_reactions = [r[0] for r in ur_res.all()]
    return {"counts": counts, "user_reactions": user_reactions}


# ─── Comments ────────────────────────────────────────────────────────────────

@app.get("/api/news/{slug}/comments", response_model=PaginatedComments)
async def get_comments(
    slug: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(status_code=404, detail="News not found")

    # Fetch every comment for this article in one go and build the reply
    # tree in memory — replies can nest to any depth (reply-to-a-reply),
    # so a single-level query would silently drop grandchild replies.
    all_res = await db.execute(
        select(Comment)
        .where(Comment.news_id == news_id)
        .order_by(Comment.created_at.asc())
        .options(selectinload(Comment.author))
    )
    all_comments = all_res.scalars().all()

    children_by_parent: dict = {}
    top_level = []
    for c in all_comments:
        if c.parent_id is None:
            top_level.append(c)
        else:
            children_by_parent.setdefault(c.parent_id, []).append(c)

    total = len(top_level)
    pages = max(1, math.ceil(total / per_page))
    page_items = top_level[(page - 1) * per_page: (page - 1) * per_page + per_page]

    def serialize_comment(c):
        return {
            "id": c.id,
            "content": c.content,
            "parent_id": c.parent_id,
            "created_at": c.created_at.isoformat(),
            "author": {"id": c.author.id, "username": c.author.username, "avatar_url": c.author.avatar_url, "role": c.author.role, "is_active": c.author.is_active, "created_at": c.author.created_at.isoformat(), "email": ""} if c.author else None,
            "replies": [serialize_comment(r) for r in children_by_parent.get(c.id, [])],
            "reactions": {},
            "user_reaction": None,
        }

    return {"items": [serialize_comment(c) for c in page_items], "total": total, "page": page, "pages": pages}


@app.post("/api/news/{slug}/comments", response_model=CommentOut, status_code=201)
@limiter.limit("20/minute")
async def add_comment(
    request: Request,
    slug: str,
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    news_res = await db.execute(select(News).where(News.slug == slug, News.published == True))
    news = news_res.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    comment = Comment(news_id=news.id, author_id=current_user.id, content=body.content, parent_id=body.parent_id)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    # eager load author
    await db.refresh(comment, ["author"])
    if body.parent_id:
        parent_c = await db.get(Comment, body.parent_id)
        if parent_c and parent_c.author_id and parent_c.author_id != current_user.id:
            db.add(Notification(
                user_id=parent_c.author_id,
                type="reply",
                data=json.dumps({
                    "comment_id": comment.id,
                    "news_slug": slug,
                    "news_title": news.title,
                    "from_username": current_user.username,
                    "preview": body.content[:100]
                }, ensure_ascii=False)
            ))
            await db.commit()
            # Send email notification
            parent_user = await db.get(User, parent_c.author_id)
            if parent_user and parent_user.email:
                # HTML-escape the comment content (freeform user text) before it goes into an
                # HTML email body — otherwise a comment like `<a href="evil">click</a>` renders
                # as a live link in the recipient's inbox, sent from the site's own address.
                safe_username = html.escape(current_user.username)
                safe_title = html.escape(news.title)
                safe_preview = html.escape(body.content[:200])
                asyncio.create_task(_send_notification_email(
                    parent_user.email,
                    f"Новый ответ на ваш комментарий — {news.title}",
                    f"{current_user.username} ответил на ваш комментарий:\n\n{body.content[:200]}\n\nНовость: {news.title}",
                    f"<p><b>{safe_username}</b> ответил на ваш комментарий в новости <b>{safe_title}</b>:</p><blockquote>{safe_preview}</blockquote>",
                ))
    return {
        "id": comment.id,
        "content": comment.content,
        "parent_id": comment.parent_id,
        "created_at": comment.created_at.isoformat(),
        "author": {"id": comment.author.id, "username": comment.author.username, "avatar_url": comment.author.avatar_url, "role": comment.author.role, "is_active": comment.author.is_active, "created_at": comment.author.created_at.isoformat(), "email": ""} if comment.author else None,
        "replies": [],
        "reactions": {},
        "user_reaction": None,
    }


@app.patch("/api/comments/{comment_id}", response_model=CommentOut)
async def update_comment(
    comment_id: int,
    body: CommentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not is_at_least(current_user, "moderator") and comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    comment.content = body.content
    await db.commit()
    await db.refresh(comment)
    return CommentOut.model_validate(comment)


@app.delete("/api/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not is_at_least(current_user, "moderator") and comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    await db.delete(comment)
    await db.commit()


# ─── Comment reactions ───────────────────────────────────────────────────────

ALLOWED_COMMENT_EMOJIS = {"👍", "❤️", "😂", "😮", "😢", "👎"}


@app.post("/api/comments/{comment_id}/react")
async def react_comment(comment_id: int, body: ReactBody, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if body.emoji not in ALLOWED_COMMENT_EMOJIS:
        raise HTTPException(400, "Invalid emoji")
    existing = await db.execute(
        select(CommentReaction).where(
            CommentReaction.comment_id == comment_id,
            CommentReaction.user_id == current_user.id,
            CommentReaction.emoji == body.emoji
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
        return {"toggled": False}
    db.add(CommentReaction(comment_id=comment_id, user_id=current_user.id, emoji=body.emoji))
    await db.commit()
    return {"toggled": True}


@app.get("/api/comments/{comment_id}/reactions")
async def get_comment_reactions(comment_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(CommentReaction.emoji, func.count(CommentReaction.id))
        .where(CommentReaction.comment_id == comment_id)
        .group_by(CommentReaction.emoji)
    )
    counts = {row[0]: row[1] for row in res.all()}
    user_reaction = None
    try:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                uid = int(payload.get("sub", 0))
                if uid:
                    ur = await db.execute(
                        select(CommentReaction.emoji).where(
                            CommentReaction.comment_id == comment_id,
                            CommentReaction.user_id == uid
                        )
                    )
                    user_reaction = ur.scalar_one_or_none()
    except Exception:
        pass
    return {"counts": counts, "user_reaction": user_reaction}


# ─── Notifications ────────────────────────────────────────────────────────────

@app.get("/api/notifications")
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


@app.post("/api/notifications/read-all")
async def mark_notifications_read(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.read == False)
        .values(read=True)
    )
    await db.commit()
    return {"ok": True}


@app.delete("/api/notifications/{notif_id}")
async def delete_notification(notif_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    n = await db.get(Notification, notif_id)
    if n and n.user_id == current_user.id:
        await db.delete(n)
        await db.commit()
    return {"ok": True}


# ─── Discord Webhook ─────────────────────────────────────────────────────────

async def _discord_webhook_news(url: str, news_title: str, news_summary: str, news_slug: str, thumb_url: str = "") -> None:
    if not url or not url.startswith("https://discord.com/api/webhooks/"):
        return
    try:
        embed = {"title": news_title[:256], "description": news_summary[:512], "color": 0xB5002A, "footer": {"text": "V Rising News"}}
        if thumb_url:
            embed["thumbnail"] = {"url": thumb_url}
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"embeds": [embed]}, timeout=5.0)
    except Exception as e:
        logger.warning("Discord webhook error: %s", e)


@app.post("/api/admin/test-webhook")
async def test_discord_webhook(request: Request, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    try:
        body_data = await request.json()
        url = (body_data.get("url") or "").strip()
    except Exception:
        url = ""
    if not url:
        res = await db.execute(select(Setting).where(Setting.key == "discord_webhook_url"))
        setting = res.scalar_one_or_none()
        url = (setting.value or "").strip() if setting else ""
    if not url or "discord" not in url or "/api/webhooks/" not in url:
        raise HTTPException(status_code=400, detail="Discord Webhook URL не настроен — введите URL в поле выше")
    try:
        embed = {
            "title": "✅ Тест вебхука — V Rising",
            "description": "Вебхук настроен корректно. Уведомления о новостях будут появляться здесь.",
            "color": 0x00B050,
            "footer": {"text": "V Rising Admin Panel"},
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json={"embeds": [embed]}, timeout=10.0)
        if r.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail=f"Discord вернул {r.status_code}: {r.text[:300]}")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса: {type(e).__name__}: {e}")


# ─── News (admin) ────────────────────────────────────────────────────────────

@app.get("/api/admin/news", response_model=PaginatedNews)
async def admin_list_news(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    total_result = await db.execute(select(func.count()).select_from(News))
    total = total_result.scalar_one()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    result = await db.execute(
        select(News).order_by(News.created_at.desc()).offset(offset).limit(per_page)
    )
    items = result.scalars().all()
    return PaginatedNews(
        items=[NewsListOut.model_validate(n) for n in items],
        total=total,
        page=page,
        pages=pages,
    )


@app.get("/api/admin/news/{news_id}", response_model=NewsOut)
async def admin_get_news(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(
        select(News).options(selectinload(News.author)).where(News.id == news_id)
    )
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    return NewsOut.model_validate(news)


@app.post("/api/admin/news", response_model=NewsOut, status_code=201)
async def create_news(
    body: NewsCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    base_slug = slugify(body.title)
    slug = base_slug
    counter = 1
    while True:
        existing = await db.execute(select(News).where(News.slug == slug))
        if existing.scalar_one_or_none() is None:
            break
        slug = f"{base_slug}-{counter}"
        counter += 1
    _published = body.published
    _publish_at = None
    if body.publish_at:
        _pa = body.publish_at
        if _pa.tzinfo is None:
            _pa = _pa.replace(tzinfo=timezone.utc)
        if _pa > datetime.now(timezone.utc):
            _published = False
            _publish_at = _pa
    news = News(
        title=body.title,
        slug=slug,
        summary=body.summary,
        content=body.content,
        thumbnail_url=body.thumbnail_url,
        tags=body.tags or "",
        author_id=current_user.id,
        published=_published,
        publish_at=_publish_at,
        is_template=body.is_template,
    )
    db.add(news)
    await db.flush()  # get news.id for audit
    await _audit(db, current_user.id, "news.create", target_type="news", target_id=news.id, detail=news.title)
    await db.commit()
    await db.refresh(news)
    if news.published:
        wh_res = await db.execute(select(Setting).where(Setting.key == "discord_webhook_url"))
        wh = wh_res.scalar_one_or_none()
        if wh and wh.value:
            asyncio.create_task(_discord_webhook_news(wh.value, news.title, news.summary, news.slug, news.thumbnail_url or ""))
    result = await db.execute(select(News).where(News.id == news.id))
    return NewsOut.model_validate(result.scalar_one())


@app.put("/api/admin/news/{news_id}", response_model=NewsOut)
async def update_news(
    news_id: int,
    body: NewsUpdate,
    db: AsyncSession = Depends(get_db),
    admin_u: User = Depends(get_admin_user),
):
    result = await db.execute(select(News).where(News.id == news_id))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    fields = body.model_fields_set
    if 'title'         in fields: news.title         = body.title
    if 'summary'       in fields: news.summary       = body.summary
    if 'content'       in fields: news.content       = body.content
    if 'thumbnail_url' in fields: news.thumbnail_url = body.thumbnail_url  # None = clear
    if 'tags'          in fields: news.tags          = body.tags
    if 'published'     in fields: news.published     = body.published
    if 'pinned'        in fields: news.pinned        = body.pinned
    if 'publish_at'    in fields:
        _pa = body.publish_at
        if _pa:
            if _pa.tzinfo is None:
                _pa = _pa.replace(tzinfo=timezone.utc)
            if _pa > datetime.now(timezone.utc):
                news.published = False
                news.publish_at = _pa
            else:
                news.publish_at = None
        else:
            news.publish_at = None
    news.updated_at = datetime.now(timezone.utc)
    await _audit(db, admin_u.id, "news.update", target_type="news", target_id=news.id, detail=news.title)
    await db.commit()
    await db.refresh(news)
    result2 = await db.execute(select(News).where(News.id == news.id))
    return NewsOut.model_validate(result2.scalar_one())


@app.delete("/api/admin/news/{news_id}", status_code=204)
async def delete_news(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    admin_u: User = Depends(get_admin_user),
):
    result = await db.execute(select(News).where(News.id == news_id))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    _news_title = news.title
    await db.delete(news)
    await _audit(db, admin_u.id, "news.delete", target_type="news", target_id=news_id, detail=_news_title)
    await db.commit()


# ─── File upload ─────────────────────────────────────────────────────────────

_ALLOWED_UPLOAD_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
_ALLOWED_UPLOAD_MIME = {
    "image/png", "image/jpeg", "image/gif",
    "image/webp", "image/x-icon", "image/vnd.microsoft.icon",
}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@app.post("/api/admin/upload")
async def upload_file(
    file: UploadFile = File(...),
    _: User = Depends(get_admin_user),
):
    # SVG deliberately excluded: it can embed <script>/event handlers, so an admin
    # upload used somewhere other than a plain <img> (e.g. an <object>/<iframe>, or a
    # future markup change) would execute same-origin against every visitor.
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, detail="Допустимые форматы: PNG, JPG, GIF, WebP, ICO")
    if file.content_type and file.content_type.split(";")[0].strip() not in _ALLOWED_UPLOAD_MIME:
        raise HTTPException(400, detail="Недопустимый MIME-тип файла")
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(400, detail="Файл слишком большой (максимум 10 МБ)")
    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / filename
    dest.write_bytes(content)
    return {"url": f"/api/uploads/{filename}"}


@app.get("/api/uploads/covers/{filename}")
async def serve_cover_upload(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(404)
    path = UPLOAD_DIR / "covers" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/uploads/{filename}")
async def serve_upload(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(404)
    path = UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=31536000, immutable"})


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


MAINTENANCE_FLAG_PATH = "/var/maintenance/.flag"


def _write_maintenance_flag(enabled: bool) -> None:
    """Write or remove the maintenance flag file for nginx."""
    import os, pathlib
    try:
        flag = pathlib.Path(MAINTENANCE_FLAG_PATH)
        if enabled:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
    except Exception:
        pass


@app.get("/api/settings/public")
async def get_public_settings(db: AsyncSession = Depends(get_db)):
    keys = ["site_title", "site_tagline", "site_description", "site_logo_url", "hero_logo_url", "hero_subtitle", "favicon_url", "discord_url", "discord_server_id", "max_url", "bg_image_url", "server_ip", "server_port", "server_name", "server2_name", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2", "event_active", "event_title", "event_text", "event_color", "rules", "timezone", "time_format", "date_format", "maintenance_mode", "maintenance_title", "maintenance_message", "maintenance_video_url", "maintenance_end_time", "maintenance_start_time", "maintenance_fallback_image", "maintenance_status_updates", "maintenance_history"]
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

@app.post("/api/admin/maintenance/status", status_code=200)
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


@app.delete("/api/admin/maintenance/status/{idx}", status_code=200)
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

@app.post("/api/admin/maintenance/extend")
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
}

@app.get("/api/admin/settings", response_model=list[SettingOut])
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(Setting))
    return [SettingOut.model_validate(s) for s in result.scalars().all()]


@app.put("/api/admin/settings/{key}", response_model=SettingOut)
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


# ─── Users (admin) ───────────────────────────────────────────────────────────

@app.get("/api/admin/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_moderator_user),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@app.put("/api/admin/users/{user_id}/role")
async def change_role(
    user_id: int,
    role: str = Query(..., regex="^(user|moderator|admin|superadmin)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_superadmin_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change own role")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.role = role
    await log_audit(db, current_user, "user.role", f"{user.username} → {role}")
    await db.commit()
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/toggle-active")
async def toggle_active(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_moderator_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if role_level(user.role) >= role_level(current_user.role):
        raise HTTPException(status_code=403, detail="Cannot act on a user with equal or higher access level")
    user.is_active = not user.is_active
    _ban_action = "user.unban" if user.is_active else "user.ban"
    await _audit(db, current_user.id, _ban_action, target_type="user", target_id=user.id, detail=user.username)
    await db.commit()
    return {"ok": True, "is_active": user.is_active}


@app.delete("/api/admin/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if role_level(user.role) >= role_level(current_user.role):
        raise HTTPException(status_code=403, detail="Cannot act on a user with equal or higher access level")
    await db.delete(user)
    await log_audit(db, current_user, "user.delete", user.username)
    await db.commit()


@app.post("/api/admin/users/{user_id}/revoke-sessions", status_code=204)
async def revoke_user_sessions(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Force-logout: set revoke_before=now() so all older tokens are rejected on next request."""
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Self-revoke is allowed (unlike delete/toggle-active) — logging yourself out is
    # self-correcting via re-login, so only guard against acting on a DIFFERENT peer/superior.
    if user_id != current_user.id and role_level(target.role) >= role_level(current_user.role):
        raise HTTPException(status_code=403, detail="Cannot act on a user with equal or higher access level")
    now_utc = datetime.now(timezone.utc).isoformat()
    await db.execute(text("UPDATE users SET revoke_before = :ts WHERE id = :uid"), {"ts": now_utc, "uid": user_id})
    await log_audit(db, current_user, "user.revoke_sessions", target.username)
    await db.commit()


# ─── Linked game accounts (admin) ────────────────────────────────────────────
# Site accounts linked to a SteamID via the in-game .register/.login flow (see
# User.steam_id, set by /api/plugin/register and /api/plugin/login above).

@app.get("/api/admin/linked-accounts", response_model=list[LinkedAccountOut])
async def list_linked_accounts(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(
        select(User).where(User.steam_id.isnot(None)).order_by(User.username.asc())
    )
    return [LinkedAccountOut.model_validate(u) for u in result.scalars().all()]


@app.post("/api/admin/users/{user_id}/unlink-steam")
async def unlink_steam_account(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Clears the SteamID link set via the in-game .register/.login flow, e.g. so the
    player can re-link a different game account. Does not touch the site account itself
    (username/password/etc.) — only the link."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.steam_id = None
    await _audit(db, current_user.id, "user.unlink_steam", target_type="user", target_id=user.id, detail=user.username)
    await db.commit()
    return {"ok": True}


# ─── Public bans list ────────────────────────────────────────────────────────

@app.get("/api/bans")
async def list_public_bans(db: AsyncSession = Depends(get_db)):
    """Public, unauthenticated list of currently-active in-game bans for bans.html's
    anonymous visitors. character_name and reason are shown deliberately: in-game
    server bans are ordinary server-transparency content (like a public
    rules-violations board), not sensitive personal data — there's no real name,
    email, or other PII involved, just a game character name and why it was banned.
    Same "active" semantics as GET /api/admin/bans's default (Ban.unbanned_at IS
    NULL) and the same row shape that endpoint returns, minus steam_id/unbanned_at
    (no reason to publish a player's SteamID, and "resolved" history has no public
    view). No unban capability here — that stays admin-only via
    POST /api/admin/bans/{id}/unban, which bans.html calls directly once it
    separately confirms admin via /api/auth/me and shows an extra action column.
    This briefly (74b07ba) returned just {"active_bans": N} instead, on the theory
    that names/reasons were too sensitive to publish — that instinct turned out not
    to match what's actually wanted for this page, so it's back to a full list."""
    server_names = await _get_server_names(db)
    result = await db.execute(
        select(Ban).where(Ban.unbanned_at.is_(None)).order_by(Ban.banned_at.desc())
    )
    bans = result.scalars().all()
    return {
        "bans": [
            {
                "id": b.id,
                "server_num": b.server_num,
                "server_name": server_names.get(b.server_num) or f"Сервер {b.server_num}",
                "character_name": b.character_name,
                "admin_name": b.admin_name,
                "reason": b.reason,
                "banned_at": _fmt_dt_z(b.banned_at),
                "unban_at": _fmt_dt_z(b.unban_at),
            }
            for b in bans
        ]
    }


# ─── Public profile ──────────────────────────────────────────────────────────

@app.get("/api/users/{username}")
async def get_public_profile(username: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.username == username, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    lookup_name = user.game_nickname or username
    total_result = await db.execute(
        select(func.sum(PlayerRecord.total_seconds)).where(PlayerRecord.player_name == lookup_name)
    )
    total_seconds = total_result.scalar_one() or 0
    last_seen_result = await db.execute(
        select(func.max(PlayerRecord.last_seen)).where(PlayerRecord.player_name == lookup_name)
    )
    last_seen = last_seen_result.scalar_one()
    session_count_result = await db.execute(
        select(func.sum(PlayerRecord.session_count)).where(PlayerRecord.player_name == lookup_name)
    )
    session_count = int(session_count_result.scalar_one() or 0)
    last_dur_result = await db.execute(
        select(PlayerRecord.last_duration).where(
            PlayerRecord.player_name == lookup_name
        ).order_by(PlayerRecord.last_seen.desc()).limit(1)
    )
    last_duration = last_dur_result.scalar_one_or_none() or 0
    # True once at least one PlayerRecord row for this player was claimed by a real
    # /api/plugin/sessions report (steam_id set), vs. total_seconds being purely an
    # older Steam-A2S-polling estimate never confirmed by the plugin.
    verified_result = await db.execute(
        select(PlayerRecord.id).where(
            PlayerRecord.player_name == lookup_name, PlayerRecord.steam_id.isnot(None)
        ).limit(1)
    )
    verified = verified_result.scalar_one_or_none() is not None
    # Clan membership now comes from the game-synced roster (matched via the verified
    # steam_id link), not the old web-managed Clan/User.clan_id system.
    clan = None
    if user.steam_id:
        clan_result = await db.execute(
            select(GameClan).join(GameClanMember, GameClanMember.clan_id == GameClan.id)
            .where(GameClanMember.steam_id == user.steam_id)
        )
        clan_row = clan_result.scalars().first()
        if clan_row:
            clan = {"id": clan_row.id, "name": clan_row.name}
    comment_count_res = await db.execute(
        select(func.count(Comment.id)).where(Comment.author_id == user.id)
    )
    comment_count = comment_count_res.scalar_one() or 0
    return {
        "username": user.username,
        "avatar_url": user.avatar_url,
        "cover_url": user.cover_url,
        "role": user.role,
        "created_at": _fmt_dt(user.created_at),
        "game_nickname": user.game_nickname,
        "total_seconds": total_seconds,
        "last_seen": _fmt_dt(last_seen),
        "session_count": session_count,
        "last_duration": last_duration,
        "verified": verified,
        "clan": clan,
        "admin_title": user.admin_title,
        "last_active_at": _fmt_dt(user.last_active_at),
        "badge_icon_url": user.badge_icon_url,
        "badge_style": user.badge_style or "default",
        "comment_count": comment_count,
    }


# ─── User activity feed ──────────────────────────────────────────────────────

@app.get("/api/users/{username}/activity")
async def get_user_activity(username: str, db: AsyncSession = Depends(get_db)):
    user_res = await db.execute(select(User).where(User.username == username, User.is_active == True))
    user = user_res.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Query last 20 comments by this user
    comments_res = await db.execute(
        select(Comment.id, Comment.content, Comment.created_at, News.slug.label("news_slug"), News.title.label("news_title"))
        .join(News, Comment.news_id == News.id)
        .where(Comment.author_id == user.id)
        .order_by(Comment.created_at.desc())
        .limit(20)
    )
    comment_rows = comments_res.all()

    # Query last 20 reactions by this user
    reactions_res = await db.execute(
        select(Reaction.id, Reaction.emoji, Reaction.created_at, News.slug.label("news_slug"), News.title.label("news_title"))
        .join(News, Reaction.news_id == News.id)
        .where(Reaction.user_id == user.id)
        .order_by(Reaction.created_at.desc())
        .limit(20)
    )
    reaction_rows = reactions_res.all()

    _strip_html = re.compile(r'<[^>]+>')
    items = []
    for r in comment_rows:
        dt = r.created_at
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        preview = _strip_html.sub('', r.content)[:120]
        items.append({"type": "comment", "created_at": _fmt_dt(dt), "news_slug": r.news_slug, "news_title": r.news_title, "preview": preview})
    for r in reaction_rows:
        dt = r.created_at
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        items.append({"type": "reaction", "created_at": _fmt_dt(dt), "news_slug": r.news_slug, "news_title": r.news_title, "emoji": r.emoji})

    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"username": username, "items": items[:20]}


# ─── Leaderboard ─────────────────────────────────────────────────────────────

@app.get("/api/leaderboard", response_model=list[PlayerRecordOut])
async def get_leaderboard(
    server: int = Query(1),
    period: str = Query("all"),
    q: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(PlayerRecord).where(PlayerRecord.server_num == server)
    if period in ("week", "month"):
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff -= timedelta(days=7 if period == "week" else 30)
        query = query.where(PlayerRecord.last_seen >= cutoff)
    if q.strip():
        query = query.where(PlayerRecord.player_name.ilike(f"%{q.strip()}%"))
    query = query.order_by(PlayerRecord.total_seconds.desc()).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    records = result.scalars().all()

    avatar_map = {}
    if records:
        names = [r.player_name for r in records]
        users_result = await db.execute(select(User.username, User.avatar_url).where(User.username.in_(names)))
        avatar_map = {u.username: u.avatar_url for u in users_result.all()}

    hist_rank_map = {}
    if period == "all" and records:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        sub = (
            select(PlayerRankSnapshot.player_name, func.max(PlayerRankSnapshot.recorded_at).label("max_ts"))
            .where(PlayerRankSnapshot.server_num == server, PlayerRankSnapshot.recorded_at <= cutoff)
            .group_by(PlayerRankSnapshot.player_name)
            .subquery()
        )
        hist_result = await db.execute(
            select(PlayerRankSnapshot.player_name, PlayerRankSnapshot.total_seconds)
            .join(sub, and_(
                PlayerRankSnapshot.player_name == sub.c.player_name,
                PlayerRankSnapshot.recorded_at == sub.c.max_ts,
            ))
            .where(PlayerRankSnapshot.server_num == server)
        )
        hist_rows = hist_result.all()
        for rank_then, (name, _secs) in enumerate(sorted(hist_rows, key=lambda row: row.total_seconds, reverse=True), start=1):
            hist_rank_map[name] = rank_then

    out = []
    for i, r in enumerate(records):
        item = PlayerRecordOut.model_validate(r)
        item.avatar_url = avatar_map.get(r.player_name)
        item.verified = r.steam_id is not None
        if period == "all" and r.player_name in hist_rank_map:
            current_rank = (page - 1) * per_page + i + 1
            item.rank_delta = hist_rank_map[r.player_name] - current_rank
        out.append(item)
    return out


@app.get("/api/leaderboard/points", response_model=list[PointsLeaderboardEntryOut])
async def get_points_leaderboard(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Points-economy leaderboard: site accounts ranked by points_balance (earned via
    playtime/streak, spent in the shop — see _award_points()) descending. Unlike the
    playtime leaderboard above this is not per-server (points_balance is a single global
    balance per User) and has no week/month period filter (it's a running balance, not a
    time-bucketed stat). Zero/negative balances and deactivated accounts are excluded,
    same spirit as the playtime leaderboard only ever having rows for players who've
    actually accrued something."""
    query = (
        select(User)
        .where(User.is_active == True, User.points_balance > 0)
        .order_by(User.points_balance.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(query)
    users = result.scalars().all()
    return [PointsLeaderboardEntryOut.model_validate(u) for u in users]


@app.delete("/api/admin/leaderboard/{record_id}", status_code=204)
async def delete_leaderboard_record(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(PlayerRecord).where(PlayerRecord.id == record_id))
    rec = result.scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="Record not found")
    await db.delete(rec)
    await db.commit()


# ─── Clans (game-synced, read-only) ───────────────────────────────────────────
# Clan data is owned by the game itself — the plugin pushes the full current roster to
# POST /api/plugin/clans/sync (see "Game Plugin Integration" above). The website only
# ever displays it; there is no web-managed create/join/leave/delete anymore.

async def _game_clan_out(db: AsyncSession, clan: GameClan, with_members: bool = False, server_names: Optional[dict] = None):
    count_result = await db.execute(
        select(func.count(GameClanMember.id)).where(GameClanMember.clan_id == clan.id)
    )
    member_count = count_result.scalar_one()
    if server_names is None:
        server_names = await _get_server_names(db)
    base = {
        "id": clan.id, "server_num": clan.server_num, "clan_guid": clan.clan_guid,
        "server_name": server_names.get(clan.server_num) or f"Сервер {clan.server_num}",
        "name": clan.name, "motto": clan.motto or "", "updated_at": clan.updated_at,
        "member_count": member_count,
    }
    if with_members:
        members_result = await db.execute(
            select(GameClanMember).where(GameClanMember.clan_id == clan.id).order_by(GameClanMember.character_name)
        )
        members = members_result.scalars().all()
        steam_ids = [m.steam_id for m in members]
        users_by_steam = {}
        if steam_ids:
            users_result = await db.execute(select(User).where(User.steam_id.in_(steam_ids)))
            users_by_steam = {u.steam_id: u for u in users_result.scalars().all()}
        member_list = []
        for m in members:
            u = users_by_steam.get(m.steam_id)
            member_list.append({
                "steam_id": m.steam_id, "character_name": m.character_name, "role": m.role,
                "username": u.username if u else None,
                "avatar_url": u.avatar_url if u else None,
            })
        base["members"] = member_list
    return base


@app.get("/api/clans", response_model=list[GameClanOut])
async def list_clans(search: Optional[str] = None, limit: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    query = select(GameClan)
    if search:
        query = query.where(GameClan.name.ilike(f"%{search}%"))
    query = query.order_by(GameClan.name)
    if limit:
        query = query.limit(limit)
    result = await db.execute(query)
    clans = result.scalars().all()
    server_names = await _get_server_names(db)
    return [await _game_clan_out(db, c, server_names=server_names) for c in clans]


@app.get("/api/clans/{clan_id}", response_model=GameClanDetailOut)
async def get_clan(clan_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(GameClan).where(GameClan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    return await _game_clan_out(db, clan, with_members=True)


# ─── System operations (admin) ────────────────────────────────────────────────

async def _stream_cmd(*cmd: str):
    """Async generator that yields decoded lines from a subprocess command."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if line:
            yield line
    await proc.wait()
    yield f"__rc__{proc.returncode}"


@app.post("/api/admin/ssl/install")
async def ssl_install(
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    import os
    import struct
    import json as _json

    result = await db.execute(
        select(Setting).where(Setting.key.in_(["https_domain", "https_email"]))
    )
    smap = {s.key: s.value for s in result.scalars()}
    domain = smap.get("https_domain", "").strip()
    email = smap.get("https_email", "").strip()
    if not domain or not email:
        raise HTTPException(400, "Заполните домен и email в настройках HTTPS")

    DOCKER_SOCK = "/var/run/docker.sock"

    async def stream():
        def sse(msg: str) -> str:
            return f"data: {msg}\n\n"

        try:
            yield sse(f"🔐 Запрашиваем сертификат Let's Encrypt для {domain}...")

            if not os.path.exists(DOCKER_SOCK):
                yield sse("❌ Docker socket не найден: /var/run/docker.sock")
                yield sse("DONE:error")
                return

            transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://docker", timeout=httpx.Timeout(300.0)
            ) as dc:

                # Pull certbot image
                yield sse("📥 Загружаем образ certbot/certbot...")
                try:
                    async with dc.stream("POST", "/images/create",
                                         params={"fromImage": "certbot/certbot", "tag": "latest"}) as resp:
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            try:
                                data = _json.loads(line)
                                status = data.get("status", "")
                                # skip noisy per-layer lines
                                if status and status not in (
                                    "Pulling fs layer", "Waiting", "Verifying Checksum",
                                    "Download complete", "Pull complete", "Already exists",
                                ):
                                    yield sse(status)
                                if "error" in data:
                                    yield sse(f"❌ {data['error']}")
                            except Exception:
                                pass
                except Exception as exc:
                    yield sse(f"❌ Ошибка загрузки образа: {exc}")
                    yield sse("DONE:error")
                    return

                # Create certbot container
                yield sse("🚀 Запускаем certbot...")
                try:
                    create_resp = await dc.post("/containers/create", json={
                        "Image": "certbot/certbot",
                        "Cmd": [
                            "certonly", "--webroot",
                            "--webroot-path=/var/www/certbot",
                            "-d", domain,
                            "--email", email,
                            "--agree-tos", "--non-interactive", "--no-eff-email",
                        ],
                        "HostConfig": {
                            "Binds": [
                                "vrising_letsencrypt:/etc/letsencrypt",
                                "vrising_certbot_webroot:/var/www/certbot",
                            ],
                        },
                    })
                    if create_resp.status_code not in (200, 201):
                        yield sse(f"❌ Ошибка создания контейнера: {create_resp.text}")
                        yield sse("DONE:error")
                        return
                    container_id = create_resp.json()["Id"]
                except Exception as exc:
                    yield sse(f"❌ Ошибка создания контейнера: {exc}")
                    yield sse("DONE:error")
                    return

                # Start
                await dc.post(f"/containers/{container_id}/start")

                # Stream logs (Docker multiplexed frame format)
                try:
                    async with dc.stream("GET", f"/containers/{container_id}/logs",
                                         params={"stdout": 1, "stderr": 1, "follow": 1}) as log_resp:
                        buf = b""
                        async for chunk in log_resp.aiter_bytes():
                            buf += chunk
                            while len(buf) >= 8:
                                frame_size = struct.unpack(">I", buf[4:8])[0]
                                if len(buf) < 8 + frame_size:
                                    break
                                payload = buf[8:8 + frame_size].decode(errors="replace").strip()
                                buf = buf[8 + frame_size:]
                                if payload:
                                    yield sse(payload)
                except Exception as exc:
                    yield sse(f"⚠ Ошибка чтения логов: {exc}")

                # Get exit code
                rc = -1
                try:
                    wait_resp = await dc.post(f"/containers/{container_id}/wait",
                                              timeout=httpx.Timeout(30.0))
                    rc = wait_resp.json().get("StatusCode", -1)
                except Exception:
                    pass

                # Cleanup container
                try:
                    await dc.delete(f"/containers/{container_id}", params={"force": True})
                except Exception:
                    pass

                if rc != 0:
                    yield sse("❌ Ошибка получения сертификата. Проверьте что A-запись домена указывает на этот сервер и порт 80 открыт.")
                    yield sse("DONE:error")
                    return

            # Update nginx config
            yield sse("📝 Обновляем конфигурацию nginx...")
            try:
                workspace = "/opt/vrising-site"
                with open(f"{workspace}/nginx/nginx-ssl.conf") as f:
                    ssl_conf = f.read().replace("DOMAIN", domain)
                with open(f"{workspace}/nginx/nginx.conf", "w") as f:
                    f.write(ssl_conf)
                yield sse(f"✅ nginx.conf обновлён для домена {domain}")
            except Exception as exc:
                yield sse(f"❌ Ошибка записи конфига: {exc}")
                yield sse("DONE:error")
                return

            # Reload nginx via Docker socket
            yield sse("🔄 Перезапускаем nginx...")
            try:
                transport2 = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
                async with httpx.AsyncClient(
                    transport=transport2, base_url="http://docker", timeout=httpx.Timeout(60.0)
                ) as dc2:
                    exec_resp = await dc2.post("/containers/vrising_nginx/exec", json={
                        "Cmd": ["nginx", "-s", "reload"],
                        "AttachStdout": True, "AttachStderr": True,
                    })
                    if exec_resp.status_code in (200, 201):
                        await dc2.post(f"/exec/{exec_resp.json()['Id']}/start",
                                       json={"Detach": True})
                        yield sse("✅ nginx перезагружен")
                    else:
                        await dc2.post("/containers/vrising_nginx/restart",
                                       timeout=httpx.Timeout(30.0))
                        yield sse("✅ nginx перезапущен")
            except Exception as exc:
                yield sse(f"⚠ Перезапуск nginx: {exc}")

            yield sse("🎉 HTTPS успешно настроен! Сайт теперь доступен по https://" + domain)
            yield sse("DONE:ok")

        except Exception as exc:
            yield sse(f"❌ Неожиданная ошибка: {exc}")
            yield sse("DONE:error")

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _git_short_hash(repo: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo, "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return out.decode().strip() or "unknown"
    except Exception:
        return "unknown"


async def _git_log_oneline(repo: str, old_hash: str, new_hash: str) -> list[str]:
    """Returns commit messages between old and new hash."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo, "log", "--oneline", f"{old_hash}..{new_hash}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        lines = [l for l in out.decode().strip().splitlines() if l]
        return lines[:10]
    except Exception:
        return []


@app.post("/api/admin/update")
async def site_update(_: User = Depends(get_superadmin_user)):
    async def stream():
        def sse(msg: str) -> str:
            return f"data: {msg}\n\n"

        repo = "/opt/vrising-site"
        old_hash = await _git_short_hash(repo)
        yield sse(f"📦 Текущая версия: {old_hash}")
        yield sse("⬇️ Получаем обновления из репозитория...")

        rc = 0
        async for line in _stream_cmd("git", "-C", repo, "pull", "--ff-only"):
            if line.startswith("__rc__"):
                rc = int(line[6:])
            else:
                yield sse(line)

        if rc != 0:
            yield sse("❌ Ошибка git pull. Убедитесь что репозиторий настроен и нет конфликтов.")
            yield sse("DONE:error")
            return

        new_hash = await _git_short_hash(repo)

        if old_hash == new_hash:
            yield sse(f"✅ Уже актуальная версия ({new_hash}). Обновлений нет.")
        else:
            yield sse(f"✅ Обновлено: {old_hash} → {new_hash}")
            commits = await _git_log_oneline(repo, old_hash, new_hash)
            if commits:
                yield sse("📋 Что изменилось:")
                for c in commits:
                    yield sse(f"  • {c}")

        yield sse("🌐 Frontend обновлён мгновенно.")
        yield sse("🔄 Backend перезагружается автоматически (uvicorn --reload)...")
        yield sse("DONE:ok")

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Dashboard stats ─────────────────────────────────────────────────────────

@app.get("/api/admin/stats")
async def admin_stats(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    user_count    = (await db.execute(select(func.count(User.id)))).scalar_one()
    news_count    = (await db.execute(select(func.count(News.id)))).scalar_one()
    comment_count = (await db.execute(select(func.count(Comment.id)))).scalar_one()
    file_count    = sum(1 for f in UPLOAD_DIR.iterdir() if f.is_file())
    recent_comments = (await db.execute(
        select(Comment, News.title.label("ntitle"), News.slug.label("nslug"),
               User.username.label("uname"))
        .join(News, Comment.news_id == News.id)
        .outerjoin(User, Comment.author_id == User.id)
        .order_by(Comment.created_at.desc()).limit(5)
    )).all()
    return {
        "user_count": user_count, "news_count": news_count,
        "comment_count": comment_count, "file_count": file_count,
        "recent_comments": [
            {"id": r.Comment.id, "content": r.Comment.content[:120],
             "news_title": r.ntitle, "news_slug": r.nslug,
             "author": r.uname or "Аноним",
             "created_at": r.Comment.created_at.isoformat()}
            for r in recent_comments
        ],
    }


# ─── Comments moderation ─────────────────────────────────────────────────────

@app.get("/api/admin/comments")
async def list_all_comments(
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    q: str = Query(""),
    news_slug: Optional[str] = Query(None),
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(Comment.content.ilike(like), User.username.ilike(like), News.title.ilike(like)))
    if news_slug:
        filters.append(News.slug == news_slug)
    count_q = select(func.count(Comment.id)).join(News, Comment.news_id == News.id).outerjoin(User, Comment.author_id == User.id).where(*filters)
    total = (await db.execute(count_q)).scalar_one()
    rows = (await db.execute(
        select(Comment, News.id.label("news_id"), News.title.label("ntitle"), News.slug.label("nslug"),
               User.id.label("uid"), User.username.label("uname"), User.avatar_url.label("uavatar"))
        .join(News, Comment.news_id == News.id)
        .outerjoin(User, Comment.author_id == User.id)
        .where(*filters)
        .order_by(Comment.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [
            {
                "id": r.Comment.id,
                "content": r.Comment.content[:200],
                "created_at": r.Comment.created_at.isoformat(),
                "user_id": r.uid,
                "username": r.uname or "Аноним",
                "avatar_url": r.uavatar,
                "news_id": r.news_id,
                "news_slug": r.nslug,
                "news_title": r.ntitle,
            }
            for r in rows
        ],
    }


@app.delete("/api/admin/comments/{comment_id}", status_code=204)
async def admin_delete_comment(
    comment_id: int,
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    await db.delete(comment)
    await db.commit()


# ─── File manager ────────────────────────────────────────────────────────────

@app.get("/api/admin/uploads")
async def list_uploads(_: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    files = []
    if not UPLOAD_DIR.exists():
        return files

    settings_rows = (await db.execute(
        select(Setting).where(Setting.key.in_(["site_logo_url", "bg_image_url"]))
    )).scalars().all()
    settings_map = {s.key: s.value for s in settings_rows}
    news_rows = (await db.execute(select(News.title, News.slug, News.thumbnail_url, News.content))).all()
    avatar_rows = (await db.execute(select(User.username, User.avatar_url).where(User.avatar_url.isnot(None)))).all()

    for f in sorted(UPLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        st = f.stat()
        used_by = []
        if settings_map.get("site_logo_url", "").endswith(f.name):
            used_by.append({"type": "logo", "label": "Логотип сайта"})
        if settings_map.get("bg_image_url", "").endswith(f.name):
            used_by.append({"type": "background", "label": "Фон сайта"})
        for title, slug, thumb, content in news_rows:
            if (thumb or "").endswith(f.name):
                used_by.append({"type": "news_thumb", "label": f"Миниатюра: {title}", "slug": slug})
            elif f.name in (content or ""):
                used_by.append({"type": "news_content", "label": f"В тексте: {title}", "slug": slug})
        for username, avatar in avatar_rows:
            if (avatar or "").endswith(f.name):
                used_by.append({"type": "avatar", "label": f"Аватар: {username}"})
        files.append({
            "filename": f.name,
            "url": f"/api/uploads/{f.name}",
            "size": st.st_size,
            "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "used_by": used_by,
        })
    return files


@app.delete("/api/admin/uploads/{filename}", status_code=204)
async def delete_upload(filename: str, _: User = Depends(get_admin_user)):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")
    path.unlink()


# ─── Media library ───────────────────────────────────────────────────────────

@app.get("/api/admin/media")
async def list_media(_: User = Depends(get_admin_user)):
    items = []
    if not UPLOAD_DIR.exists():
        return {"items": items}
    # scan root-level files
    for f in UPLOAD_DIR.iterdir():
        if f.is_file():
            st = f.stat()
            items.append({
                "filename": f.name,
                "url": f"/api/uploads/{f.name}",
                "size_bytes": st.st_size,
                "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
    # scan one level of subdirectories
    for subdir in UPLOAD_DIR.iterdir():
        if subdir.is_dir():
            for f in subdir.iterdir():
                if f.is_file():
                    rel = f"{subdir.name}/{f.name}"
                    st = f.stat()
                    items.append({
                        "filename": f.name,
                        "url": f"/api/uploads/{rel}",
                        "size_bytes": st.st_size,
                        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    })
    items.sort(key=lambda x: x["modified_at"], reverse=True)
    return {"items": items}


@app.delete("/api/admin/media/{filename:path}", status_code=200)
async def delete_media(filename: str, _: User = Depends(get_admin_user)):
    if ".." in filename or os.path.isabs(filename):
        raise HTTPException(400, "Invalid filename")
    full_path = UPLOAD_DIR / filename
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(404, "File not found")
    full_path.unlink()
    return {"ok": True}


# ─── Settings import ─────────────────────────────────────────────────────────

@app.post("/api/admin/settings/import")
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


# ─── DB Backup ───────────────────────────────────────────────────────────────

@app.get("/api/admin/backup")
async def download_backup(current_user: User = Depends(get_superadmin_user)):
    for candidate in [Path("backend/vrising.db"), Path("vrising.db"), Path("/app/backend/vrising.db")]:
        if candidate.exists():
            return FileResponse(
                path=str(candidate),
                media_type="application/octet-stream",
                filename=f"vrising_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
            )
    raise HTTPException(404, "Database file not found")


# ─── RCON ────────────────────────────────────────────────────────────────────

import struct


async def _rcon_exec(ip: str, port: int, password: str, command: str, timeout: float = 5.0) -> str:
    def _pack(pid: int, ptype: int, body: str) -> bytes:
        b = body.encode() + b"\x00\x00"
        return struct.pack("<iii", 4 + 4 + len(b), pid, ptype) + b

    async def _read(r) -> tuple:
        sz = struct.unpack("<i", await asyncio.wait_for(r.readexactly(4), timeout))[0]
        d = await asyncio.wait_for(r.readexactly(sz), timeout)
        pid, pt = struct.unpack("<ii", d[:8])
        return pid, pt, d[8:].rstrip(b"\x00").decode("utf-8", errors="replace")

    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    try:
        writer.write(_pack(1, 3, password)); await writer.drain()
        await _read(reader)
        pid, _, _ = await _read(reader)
        if pid == -1:
            raise ValueError("RCON: неверный пароль")
        writer.write(_pack(2, 2, command)); await writer.drain()
        _, _, resp = await _read(reader)
        return resp or "(пустой ответ)"
    finally:
        writer.close()
        try: await writer.wait_closed()
        except: pass


class RconBody(BaseModel):
    server: int = 1
    command: str


@app.post("/api/admin/rcon")
async def admin_rcon(body: RconBody, current_user: User = Depends(get_superadmin_user), db: AsyncSession = Depends(get_db)):
    if not body.command.strip():
        raise HTTPException(400, "Empty command")
    res = await db.execute(select(Setting).where(Setting.key.in_(["server_ip","rcon_port","rcon_password","server2_ip","rcon2_port","rcon2_password"])))
    cfg = {s.key: s.value for s in res.scalars().all()}
    if body.server == 2:
        ip, port, pw = cfg.get("server2_ip","127.0.0.1"), int(cfg.get("rcon2_port") or 25575), cfg.get("rcon2_password","")
    else:
        ip, port, pw = cfg.get("server_ip","127.0.0.1"), int(cfg.get("rcon_port") or 25575), cfg.get("rcon_password","")
    if not pw:
        raise HTTPException(400, "RCON пароль не задан в настройках")
    try:
        result = await _rcon_exec(ip, port, pw, body.command)
        await log_audit(db, current_user, "rcon_command", f"srv={body.server} cmd={body.command[:100]}")
        await db.commit()
        return {"output": result}
    except ValueError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(503, f"RCON ошибка: {e}")


# ─── Audit log ───────────────────────────────────────────────────────────────

@app.get("/api/admin/audit-log/actions")
async def get_audit_log_actions(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action))).scalars().all()
    return rows


@app.get("/api/admin/audit-log")
async def get_audit_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    q: str = Query(""),
    action: str = Query(""),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(AuditLog.admin_username.ilike(like), AuditLog.detail.ilike(like)))
    if action.strip():
        filters.append(AuditLog.action == action.strip())
    total = (await db.execute(select(func.count(AuditLog.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(AuditLog).where(*filters).order_by(AuditLog.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [
            {
                "id": r.id,
                "admin": r.admin_username,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


# ─── Events & Tournaments ─────────────────────────────────────────────────────

class EventCreate(BaseModel):
    title: str
    description: Optional[str] = None
    event_type: str = "pvp"
    start_date: datetime
    end_date: Optional[datetime] = None
    max_participants: Optional[int] = None
    cover_url: Optional[str] = None


class EventUpdate(EventCreate):
    status: Optional[str] = None


@app.get("/api/events")
async def list_events(
    request: Request,
    status: str = Query("upcoming"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if status:
        filters.append(Event.status == status)
    total = (await db.execute(select(func.count(Event.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(Event).where(*filters)
        .order_by(Event.start_date.asc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()

    # Resolve current user if authenticated
    current_user_id: Optional[int] = None
    try:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                current_user_id = int(payload.get("sub", 0)) or None
    except Exception:
        pass

    items = []
    for ev in rows:
        cnt = (await db.execute(
            select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == ev.id)
        )).scalar_one()
        is_joined = False
        if current_user_id:
            ep = (await db.execute(
                select(EventParticipant).where(
                    EventParticipant.event_id == ev.id,
                    EventParticipant.user_id == current_user_id,
                )
            )).scalar_one_or_none()
            is_joined = ep is not None
        items.append({
            "id": ev.id, "title": ev.title, "description": ev.description,
            "event_type": ev.event_type, "start_date": _fmt_dt(ev.start_date),
            "end_date": _fmt_dt(ev.end_date), "max_participants": ev.max_participants,
            "status": ev.status, "cover_url": ev.cover_url,
            "created_by": ev.created_by, "created_at": _fmt_dt(ev.created_at),
            "participant_count": cnt, "is_joined": is_joined,
        })
    return {"items": items, "total": total}


@app.get("/api/events/{event_id}")
async def get_event(
    event_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    cnt = (await db.execute(
        select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == ev.id)
    )).scalar_one()
    current_user_id: Optional[int] = None
    try:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            revoked = await db.execute(select(RevokedToken.id).where(RevokedToken.token == token))
            if revoked.scalar_one_or_none() is None:
                payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                current_user_id = int(payload.get("sub", 0)) or None
    except Exception:
        pass
    is_joined = False
    if current_user_id:
        ep = (await db.execute(
            select(EventParticipant).where(
                EventParticipant.event_id == ev.id,
                EventParticipant.user_id == current_user_id,
            )
        )).scalar_one_or_none()
        is_joined = ep is not None
    return {
        "id": ev.id, "title": ev.title, "description": ev.description,
        "event_type": ev.event_type, "start_date": _fmt_dt(ev.start_date),
        "end_date": _fmt_dt(ev.end_date), "max_participants": ev.max_participants,
        "status": ev.status, "cover_url": ev.cover_url,
        "created_by": ev.created_by, "created_at": _fmt_dt(ev.created_at),
        "participant_count": cnt, "is_joined": is_joined,
    }


@app.post("/api/admin/events", status_code=201)
async def admin_create_event(
    body: EventCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = Event(
        title=body.title[:200],
        description=body.description,
        event_type=body.event_type or "pvp",
        start_date=body.start_date,
        end_date=body.end_date,
        max_participants=body.max_participants,
        cover_url=body.cover_url,
        created_by=current_user.id,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    await _audit(db, current_user.id, "event.create", target_type="event", target_id=ev.id, detail=ev.title)
    await db.commit()
    return {"id": ev.id, "title": ev.title, "status": ev.status, "event_type": ev.event_type}


@app.put("/api/admin/events/{event_id}")
async def admin_update_event(
    event_id: int,
    body: EventUpdate,
    admin_u: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    ev.title = body.title[:200]
    ev.description = body.description
    ev.event_type = body.event_type or "pvp"
    ev.start_date = body.start_date
    ev.end_date = body.end_date
    ev.max_participants = body.max_participants
    ev.cover_url = body.cover_url
    if body.status:
        ev.status = body.status
    await _audit(db, admin_u.id, "event.update", target_type="event", target_id=ev.id, detail=ev.title)
    await db.commit()
    await db.refresh(ev)
    cnt = (await db.execute(
        select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == ev.id)
    )).scalar_one()
    return {
        "id": ev.id, "title": ev.title, "description": ev.description,
        "event_type": ev.event_type, "start_date": _fmt_dt(ev.start_date),
        "end_date": _fmt_dt(ev.end_date), "max_participants": ev.max_participants,
        "status": ev.status, "cover_url": ev.cover_url,
        "created_by": ev.created_by, "created_at": _fmt_dt(ev.created_at),
        "participant_count": cnt,
    }


@app.delete("/api/admin/events/{event_id}", status_code=204)
async def admin_delete_event(
    event_id: int,
    admin_u: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    await db.execute(delete(EventParticipant).where(EventParticipant.event_id == event_id))
    await _audit(db, admin_u.id, "event.delete", target_type="event", target_id=event_id, detail=ev.title)
    await db.delete(ev)
    await db.commit()


@app.post("/api/events/{event_id}/join")
async def join_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    if ev.status in ("ended", "cancelled"):
        raise HTTPException(400, "Event is no longer accepting participants")
    existing = (await db.execute(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already joined this event")
    if ev.max_participants is not None:
        cnt = (await db.execute(
            select(func.count(EventParticipant.user_id)).where(EventParticipant.event_id == event_id)
        )).scalar_one()
        if cnt >= ev.max_participants:
            raise HTTPException(400, "Event is full")
    db.add(EventParticipant(event_id=event_id, user_id=current_user.id))
    await db.commit()
    return {"ok": True}


@app.delete("/api/events/{event_id}/leave", status_code=204)
async def leave_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ep = (await db.execute(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if ep is None:
        raise HTTPException(404, "Not a participant")
    await db.delete(ep)
    await db.commit()


@app.get("/api/admin/events/{event_id}/participants")
async def admin_event_participants(
    event_id: int,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    ev = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
    if ev is None:
        raise HTTPException(404, "Event not found")
    rows = (await db.execute(
        select(EventParticipant).where(EventParticipant.event_id == event_id)
        .order_by(EventParticipant.registered_at.asc())
    )).scalars().all()
    result = []
    for ep in rows:
        u = await db.get(User, ep.user_id)
        result.append({
            "user_id": ep.user_id,
            "username": u.username if u else str(ep.user_id),
            "avatar_url": u.avatar_url if u else None,
            "registered_at": _fmt_dt(ep.registered_at),
        })
    return result


# ─── Background tasks ────────────────────────────────────────────────────────

async def _scheduled_publish_task():
    """Publish scheduled news posts when their publish_at time is reached."""
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            async with AsyncSession(engine, expire_on_commit=False) as db:
                rows = (await db.execute(
                    select(News).where(
                        News.published == False,
                        News.is_template == False,
                        News.publish_at != None,
                        News.publish_at <= now,
                    )
                )).scalars().all()
                for n in rows:
                    n.published = True
                    n.publish_at = None
                    logger.info("Auto-published news id=%s slug=%s", n.id, n.slug)
                if rows:
                    await db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("_scheduled_publish_task error: %s", e)


async def _scheduler_task():
    """Scheduled publishing task (15s initial delay, 60s interval)."""
    await asyncio.sleep(15)
    while True:
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                now = datetime.now(timezone.utc)
                now_naive = now.replace(tzinfo=None)

                # Auto-publish scheduled news
                result = await db.execute(
                    select(News).where(
                        News.published == False,
                        News.publish_at.isnot(None),
                        News.publish_at <= now
                    )
                )
                items = result.scalars().all()
                for news_item in items:
                    news_item.published = True
                    news_item.publish_at = None
                    db.add(news_item)
                    logger.info("_scheduler_task: auto-published news id=%s", news_item.id)
                if items:
                    await db.commit()

                # Auto-update event statuses
                try:
                    # upcoming → active when start_date <= now
                    upcoming_res = await db.execute(
                        select(Event).where(
                            Event.status == "upcoming",
                            Event.start_date <= now_naive,
                        )
                    )
                    for ev in upcoming_res.scalars().all():
                        ev.status = "active"
                        logger.info("_scheduler_task: event id=%s → active", ev.id)

                    # active → ended when end_date <= now
                    active_res = await db.execute(
                        select(Event).where(
                            Event.status == "active",
                            Event.end_date.isnot(None),
                            Event.end_date <= now_naive,
                        )
                    )
                    for ev in active_res.scalars().all():
                        ev.status = "ended"
                        logger.info("_scheduler_task: event id=%s → ended", ev.id)

                    await db.commit()
                except Exception as ev_err:
                    logger.error("_scheduler_task event update error: %s", ev_err)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("_scheduler_task error: %s", e)
        await asyncio.sleep(60)


BACKUP_DIR = Path("/data/backups")


async def _auto_backup_task():
    """Create daily DB backup at midnight UTC."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep((next_midnight - now).total_seconds())
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            db_candidates = [Path("backend/vrising.db"), Path("vrising.db"), Path("/app/backend/vrising.db"), Path("/data/vrising.db")]
            src = next((p for p in db_candidates if p.exists()), None)
            if src:
                import shutil
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                dst = BACKUP_DIR / f"vrising_{ts}.db"
                shutil.copy2(str(src), str(dst))
                logger.info("Auto backup created: %s", dst)
                # keep last 7 backups
                backups = sorted(BACKUP_DIR.glob("vrising_*.db"))
                for old in backups[:-7]:
                    old.unlink(missing_ok=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("_auto_backup_task error: %s", e)


async def _cleanup_task():
    """Nightly: purge expired revoked tokens, old page_views, old error_logs."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            next_run = (now + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
            await asyncio.sleep((next_run - now).total_seconds())
            async with AsyncSession(engine, expire_on_commit=False) as db:
                cutoff_views = datetime.now(timezone.utc) - timedelta(days=90)
                cutoff_errors = datetime.now(timezone.utc) - timedelta(days=30)
                r1 = await db.execute(delete(RevokedToken).where(RevokedToken.expires_at < datetime.now(timezone.utc)))
                r2 = await db.execute(delete(PageView).where(PageView.created_at < cutoff_views))
                r3 = await db.execute(delete(ErrorLog).where(ErrorLog.created_at < cutoff_errors))
                await db.commit()
                logger.info("Cleanup: revoked=%d page_views=%d error_logs=%d", r1.rowcount, r2.rowcount, r3.rowcount)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("_cleanup_task error: %s", e)


async def _leaderboard_snapshot_task():
    """Nightly: record each player's total_seconds so we can compute rank deltas ~7 days later."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            next_run = (now + timedelta(days=1)).replace(hour=0, minute=15, second=0, microsecond=0)
            await asyncio.sleep((next_run - now).total_seconds())
            async with AsyncSession(engine, expire_on_commit=False) as db:
                now_ts = datetime.now(timezone.utc)
                for server_num in (1, 2):
                    result = await db.execute(select(PlayerRecord).where(PlayerRecord.server_num == server_num))
                    records = result.scalars().all()
                    for r in records:
                        db.add(PlayerRankSnapshot(server_num=server_num, player_name=r.player_name, total_seconds=r.total_seconds, recorded_at=now_ts))
                await db.commit()
                logger.info("Leaderboard rank snapshot recorded for %d server(s)", 2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("_leaderboard_snapshot_task error: %s", e)


async def _monitor_poll_cycle():
    all_keys = ["server_ip", "server_port", "server_name",
                "server2_ip", "server2_port", "server2_name"]
    async with AsyncSession(engine, expire_on_commit=False) as db:
        res = await db.execute(select(Setting).where(Setting.key.in_(all_keys)))
        cfg = {s.key: s.value for s in res.scalars().all()}
    for server_num, ip_key, port_key, name_key in [
        (1, "server_ip",  "server_port",  "server_name"),
        (2, "server2_ip", "server2_port", "server2_name"),
    ]:
        ip       = cfg.get(ip_key, "").strip()
        port_str = cfg.get(port_key, "0").strip()
        if not ip or not port_str.isdigit():
            continue
        try:
            data = await get_server_status(ip, int(port_str))
        except Exception:
            continue
        admin_name = cfg.get(name_key, "").strip()
        if admin_name:
            data = {**data, "name": admin_name}
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await _track_players(db, data.get("players_list", []), server_num)
            _last_snapshot[server_num] = 0  # bypass TTL — task owns timing
            await _save_snapshot(db, data, server_num)
        # Server 2's HTTP endpoint carries an `enabled` flag; keep the cached
        # and broadcast payloads in the same shape so /status2 (served from this
        # cache for up to STATUS_CACHE_TTL) and SSE pushes never drop the field
        # — a missing `enabled` made the client hide the whole server-2 block.
        payload = {**data, "enabled": True} if server_num == 2 else data
        _status_cache[server_num] = payload
        _status_cache_ts[server_num] = time.time()
        _broadcast_status({"server": server_num, **payload})


async def _monitor_poll_task():
    """Poll game servers every 5 min so snapshots stay current even with no browsers open.

    A hard 60s timeout bounds each cycle so a stuck DB connection or socket call
    can't silently freeze this loop forever — a past incident left snapshots
    stalled for hours with no error visible until the next process restart.
    """
    await asyncio.sleep(30)  # let startup finish
    while True:
        try:
            await asyncio.wait_for(_monitor_poll_cycle(), timeout=60)
        except asyncio.CancelledError:
            break
        except asyncio.TimeoutError:
            logger.error("_monitor_poll_task cycle timed out after 60s — skipping this round")
        except Exception as e:
            logger.error("_monitor_poll_task error: %s", e)
        await asyncio.sleep(SNAPSHOT_INTERVAL)


# ─── Game nickname ────────────────────────────────────────────────────────────

class GameNicknameBody(BaseModel):
    game_nickname: str


@app.put("/api/profile/game-nickname")
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

@app.get("/api/team")
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


@app.put("/api/profile/admin-title")
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


@app.post("/api/profile/badge-icon")
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


@app.delete("/api/profile/badge-icon", status_code=204)
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


@app.put("/api/profile/badge-style")
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


# ─── Direct Messages ─────────────────────────────────────────────────────────

class MessageSendBody(BaseModel):
    recipient_username: str
    content: str

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        v = strip_html_tags(v).strip()
        if not v:
            raise ValueError("Сообщение не может быть пустым")
        if len(v) > 2000:
            raise ValueError("Максимум 2000 символов")
        return v


@app.post("/api/messages", status_code=201)
@limiter.limit("30/minute")
async def send_message(
    request: Request,
    body: MessageSendBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.recipient_username == current_user.username:
        raise HTTPException(status_code=400, detail="Нельзя писать самому себе")
    res = await db.execute(select(User).where(User.username == body.recipient_username, User.is_active == True))
    recipient = res.scalar_one_or_none()
    if recipient is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    msg = Message(sender_id=current_user.id, recipient_id=recipient.id, content=body.content.strip())
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return {
        "id": msg.id,
        "sender": current_user.username,
        "recipient": recipient.username,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
    }


@app.get("/api/messages/unread-count")
async def messages_unread_count(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(func.count()).where(Message.recipient_id == current_user.id, Message.read == False)
    )
    return {"count": res.scalar_one() or 0}


@app.get("/api/messages/inbox")
async def messages_inbox(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    partner_ids_q = (
        select(
            func.coalesce(
                func.nullif(Message.sender_id, current_user.id),
                Message.recipient_id
            ).label("partner_id"),
            func.max(Message.id).label("last_msg_id"),
        )
        .where((Message.sender_id == current_user.id) | (Message.recipient_id == current_user.id))
        .group_by("partner_id")
        .order_by(func.max(Message.id).desc())
    )
    rows = (await db.execute(partner_ids_q)).all()

    conversations = []
    for row in rows:
        partner_id = row.partner_id
        last_msg_id = row.last_msg_id
        partner_res = await db.execute(select(User).where(User.id == partner_id))
        partner = partner_res.scalar_one_or_none()
        if partner is None:
            continue
        msg_res = await db.execute(select(Message).where(Message.id == last_msg_id))
        last_msg = msg_res.scalar_one_or_none()
        unread_res = await db.execute(
            select(func.count()).where(
                Message.sender_id == partner_id,
                Message.recipient_id == current_user.id,
                Message.read == False,
            )
        )
        unread = unread_res.scalar_one() or 0
        conversations.append({
            "partner": {"id": partner.id, "username": partner.username, "avatar_url": partner.avatar_url},
            "last_message": {
                "id": last_msg.id,
                "content": last_msg.content,
                "sender_id": last_msg.sender_id,
                "created_at": last_msg.created_at.isoformat(),
            } if last_msg else None,
            "unread": unread,
        })
    return conversations


@app.get("/api/messages/with/{username}")
async def messages_conversation(
    username: str,
    before_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(User).where(User.username == username, User.is_active == True))
    partner = res.scalar_one_or_none()
    if partner is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    base = (
        select(Message)
        .where(
            ((Message.sender_id == current_user.id) & (Message.recipient_id == partner.id))
            | ((Message.sender_id == partner.id) & (Message.recipient_id == current_user.id))
        )
    )
    if before_id:
        base = base.where(Message.id < before_id)
    msgs_res = await db.execute(base.order_by(Message.id.desc()).limit(51))
    batch = msgs_res.scalars().all()
    has_more = len(batch) > 50
    messages = list(reversed(batch[:50]))

    for m in messages:
        if m.recipient_id == current_user.id and not m.read:
            m.read = True
    await db.commit()

    return {
        "partner": {"id": partner.id, "username": partner.username, "avatar_url": partner.avatar_url},
        "has_more": has_more,
        "messages": [
            {
                "id": m.id,
                "sender": m.sender.username,
                "content": m.content,
                "read": m.read,
                "created_at": m.created_at.isoformat(),
                "is_mine": m.sender_id == current_user.id,
            }
            for m in messages
        ],
    }


@app.delete("/api/messages/{msg_id}", status_code=204)
async def delete_message(
    msg_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Message).where(Message.id == msg_id, Message.sender_id == current_user.id))
    msg = res.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    await db.delete(msg)
    await db.commit()


# ─── Reports ─────────────────────────────────────────────────────────────────

@app.post("/api/reports", status_code=201)
@limiter.limit("5/minute")
async def create_report(
    request: Request,
    body: ReportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = Report(
        reporter_id=current_user.id,
        target_type=body.target_type,
        target_id=body.target_id,
        reason=body.reason,
    )
    db.add(report)
    await db.commit()
    return {"ok": True, "id": report.id}


@app.get("/api/admin/reports")
async def list_reports(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: str = Query(""),
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if status.strip():
        filters.append(Report.status == status.strip())
    total = (await db.execute(select(func.count(Report.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(Report).where(*filters).order_by(Report.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id, "reporter_id": r.reporter_id,
                "target_type": r.target_type, "target_id": r.target_id,
                "reason": r.reason, "status": r.status,
                "admin_note": r.admin_note,
                "created_at": r.created_at.isoformat(),
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            }
            for r in rows
        ],
    }


@app.patch("/api/admin/reports/{report_id}")
async def review_report(
    report_id: int,
    body: ReportReview,
    current_user: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    r = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Report not found")
    r.status = body.status
    r.admin_note = body.admin_note
    r.reviewed_at = datetime.now(timezone.utc)
    await log_audit(db, current_user, "review_report", f"id={report_id} status={body.status}")
    await db.commit()
    return {"ok": True}


# ─── Polls ────────────────────────────────────────────────────────────────────

@app.post("/api/news/{slug}/poll", status_code=201)
async def create_poll(
    slug: str,
    body: PollCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    existing = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Poll already exists for this news")
    poll = Poll(
        news_id=news.id,
        question=body.question,
        multiple=body.multiple,
        ends_at=body.ends_at,
    )
    db.add(poll)
    await db.flush()
    for opt in body.options:
        db.add(PollOption(poll_id=poll.id, text=opt.text))
    await db.commit()
    return {"ok": True, "poll_id": poll.id}


@app.get("/api/news/{slug}/poll")
async def get_poll(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    poll = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if not poll:
        return None
    # count votes per option
    vote_rows = (await db.execute(
        select(PollVote.option_id, func.count(PollVote.id).label("cnt"))
        .where(PollVote.poll_id == poll.id)
        .group_by(PollVote.option_id)
    )).all()
    vote_map = {r.option_id: r.cnt for r in vote_rows}
    total_votes = sum(vote_map.values())

    # get user voted options
    user_voted: list[int] = []
    try:
        current_user = await get_current_user(request=request, db=db)
        if current_user:
            uv = (await db.execute(
                select(PollVote.option_id).where(PollVote.poll_id == poll.id, PollVote.user_id == current_user.id)
            )).scalars().all()
            user_voted = list(uv)
    except Exception:
        pass

    return {
        "id": poll.id,
        "news_id": poll.news_id,
        "question": poll.question,
        "multiple": poll.multiple,
        "ends_at": poll.ends_at.isoformat() if poll.ends_at else None,
        "created_at": poll.created_at.isoformat(),
        "total_votes": total_votes,
        "user_voted": user_voted,
        "options": [
            {"id": o.id, "text": o.text, "votes": vote_map.get(o.id, 0)}
            for o in poll.options
        ],
    }


@app.post("/api/news/{slug}/poll/vote")
@limiter.limit("10/minute")
async def vote_poll(
    request: Request,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    option_ids = body.get("option_ids", [])
    if not option_ids:
        raise HTTPException(400, "option_ids required")

    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    poll = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if not poll:
        raise HTTPException(404, "Poll not found")
    _ends_at = poll.ends_at.replace(tzinfo=timezone.utc) if poll.ends_at and poll.ends_at.tzinfo is None else poll.ends_at
    if _ends_at and datetime.now(timezone.utc) > _ends_at:
        raise HTTPException(400, "Poll has ended")

    existing = (await db.execute(
        select(PollVote).where(PollVote.poll_id == poll.id, PollVote.user_id == current_user.id)
    )).scalars().all()
    if existing:
        raise HTTPException(400, "Already voted")

    if not poll.multiple:
        option_ids = option_ids[:1]

    for oid in option_ids:
        opt = (await db.execute(select(PollOption).where(PollOption.id == oid, PollOption.poll_id == poll.id))).scalar_one_or_none()
        if not opt:
            raise HTTPException(400, f"Invalid option id {oid}")
        db.add(PollVote(poll_id=poll.id, option_id=oid, user_id=current_user.id))
    await db.commit()
    return {"ok": True}


@app.delete("/api/news/{slug}/poll", status_code=204)
async def delete_poll(
    slug: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    news = (await db.execute(select(News).where(News.slug == slug))).scalar_one_or_none()
    if not news:
        raise HTTPException(404, "News not found")
    poll = (await db.execute(select(Poll).where(Poll.news_id == news.id))).scalar_one_or_none()
    if poll:
        await db.delete(poll)
        await db.commit()


# ─── Analytics (page views) ───────────────────────────────────────────────────

@app.get("/api/admin/analytics")
async def get_analytics(
    days: int = Query(7, ge=1, le=90),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(
            func.date(PageView.created_at).label("day"),
            func.count(PageView.id).label("views"),
            func.count(func.distinct(PageView.ip_hash)).label("unique"),
        )
        .where(PageView.created_at >= cutoff)
        .group_by(func.date(PageView.created_at))
        .order_by(func.date(PageView.created_at).asc())
    )).all()

    top_pages = (await db.execute(
        select(PageView.path, func.count(PageView.id).label("cnt"))
        .where(PageView.created_at >= cutoff)
        .group_by(PageView.path)
        .order_by(func.count(PageView.id).desc())
        .limit(10)
    )).all()

    total_views = (await db.execute(
        select(func.count(PageView.id)).where(PageView.created_at >= cutoff)
    )).scalar_one()

    # Extended analytics: totals, users_by_day, top_news
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    thirty_days_ago_naive = thirty_days_ago.replace(tzinfo=None)
    seven_days_ago_naive = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)

    total_users = (await db.execute(select(func.count(User.id)))).scalar_one()
    total_news_count = (await db.execute(
        select(func.count(News.id)).where(News.published == True)
    )).scalar_one()
    total_comments_count = (await db.execute(select(func.count(Comment.id)))).scalar_one()
    active_users_7d = (await db.execute(
        select(func.count(User.id)).where(
            User.last_active_at.isnot(None),
            User.last_active_at >= seven_days_ago_naive,
        )
    )).scalar_one()

    users_by_day_rows = (await db.execute(
        select(
            func.strftime("%Y-%m-%d", User.created_at).label("date"),
            func.count(User.id).label("count"),
        )
        .where(User.created_at >= thirty_days_ago_naive)
        .group_by(func.strftime("%Y-%m-%d", User.created_at))
        .order_by(func.strftime("%Y-%m-%d", User.created_at).asc())
    )).all()

    top_news_rows = (await db.execute(
        select(
            News.slug, News.title, News.views,
            func.count(Comment.id).label("comment_count"),
        )
        .outerjoin(Comment, News.id == Comment.news_id)
        .where(News.published == True)
        .group_by(News.id)
        .order_by(News.views.desc())
        .limit(10)
    )).all()

    return {
        "days": days,
        "total_views": total_views,
        "by_day": [{"day": r.day, "views": r.views, "unique": r.unique} for r in rows],
        "top_pages": [{"path": r.path, "views": r.cnt} for r in top_pages],
        "totals": {
            "users": total_users,
            "news": total_news_count,
            "comments": total_comments_count,
            "active_users_7d": active_users_7d,
        },
        "users_by_day": [{"date": r.date, "count": r.count} for r in users_by_day_rows],
        "top_news": [
            {"slug": r.slug, "title": r.title, "views": r.views, "comment_count": r.comment_count}
            for r in top_news_rows
        ],
    }


# ─── CSV export ───────────────────────────────────────────────────────────────

@app.get("/api/admin/export/users")
async def export_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "email", "role", "is_active", "created_at", "last_active_at"])
    for u in users:
        w.writerow([u.id, u.username, u.email, u.role, u.is_active, u.created_at, u.last_active_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


@app.get("/api/admin/export/audit-log")
async def export_audit_log(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = (await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc())
    )).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "admin_username", "action", "target_type", "target_id", "detail", "created_at"])
    for r in rows:
        w.writerow([r.id, r.admin_username, r.action, r.target_type, r.target_id, r.detail, r.created_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )


@app.get("/api/admin/export/bans")
async def export_bans(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_moderator_user),
):
    # Banned users are those with is_active=False
    rows = (await db.execute(
        select(User).where(User.is_active == False).order_by(User.created_at.desc())
    )).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "email", "role", "created_at"])
    for u in rows:
        w.writerow([u.id, u.username, u.email, u.role, u.created_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bans.csv"},
    )


# ─── Error log ────────────────────────────────────────────────────────────────

@app.get("/api/admin/errors")
async def get_error_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(ErrorLog.id)))).scalar_one()
    rows = (await db.execute(
        select(ErrorLog).order_by(ErrorLog.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {
        "total": total,
        "items": [
            {"id": r.id, "path": r.path, "method": r.method,
             "status_code": r.status_code, "error": r.error,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }


# ─── Auto backups list ────────────────────────────────────────────────────────

@app.get("/api/admin/backups")
async def list_backups(_: User = Depends(get_superadmin_user)):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("vrising_*.db"), reverse=True)
    return [
        {"filename": f.name, "size": f.stat().st_size,
         "created_at": datetime.utcfromtimestamp(f.stat().st_mtime).isoformat()}
        for f in files
    ]


@app.get("/api/admin/backups/{filename}")
async def download_named_backup(filename: str, _: User = Depends(get_superadmin_user)):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = BACKUP_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Backup not found")
    return FileResponse(path=str(path), media_type="application/octet-stream", filename=filename)


@app.post("/api/admin/backups/create", status_code=201)
async def create_backup_now(current_user: User = Depends(get_superadmin_user)):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db_candidates = [Path("backend/vrising.db"), Path("vrising.db"), Path("/app/backend/vrising.db"), Path("/data/vrising.db")]
    src = next((p for p in db_candidates if p.exists()), None)
    if not src:
        raise HTTPException(404, "Database file not found")
    import shutil
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"vrising_{ts}.db"
    shutil.copy2(str(src), str(dst))
    return {"filename": dst.name, "size": dst.stat().st_size}


# ─── Bulk user actions ────────────────────────────────────────────────────────

class BulkUserAction(BaseModel):
    user_ids: list[int]
    action: str  # "ban", "unban", "delete"


@app.post("/api/admin/users/bulk")
async def bulk_user_action(
    body: BulkUserAction,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if body.action not in ("ban", "unban", "delete"):
        raise HTTPException(400, "Invalid action")
    if not body.user_ids:
        raise HTTPException(400, "No user IDs provided")
    if len(body.user_ids) > 100:
        raise HTTPException(400, "Too many users (max 100)")

    candidates = (await db.execute(
        select(User).where(User.id.in_(body.user_ids))
    )).scalars().all()
    # Role is a plain string column — filter the hierarchy rule in Python rather than SQL.
    # user_ids is capped at 100 above, so this is cheap.
    rows = [u for u in candidates if role_level(u.role) < role_level(current_user.role)]

    affected = 0
    for u in rows:
        if body.action == "ban":
            u.is_active = False
            affected += 1
        elif body.action == "unban":
            u.is_active = True
            affected += 1
        elif body.action == "delete":
            await db.delete(u)
            affected += 1

    await log_audit(db, current_user, f"bulk_{body.action}", f"ids={body.user_ids} affected={affected}")
    await db.commit()
    return {"affected": affected}


# ─── News templates ───────────────────────────────────────────────────────────

@app.get("/api/admin/news/templates")
async def list_templates(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(News).where(News.is_template == True).order_by(News.created_at.desc())
    )).scalars().all()
    return [
        {"id": r.id, "title": r.title, "summary": r.summary,
         "content": r.content, "thumbnail_url": r.thumbnail_url,
         "tags": r.tags, "created_at": r.created_at.isoformat()}
        for r in rows
    ]
