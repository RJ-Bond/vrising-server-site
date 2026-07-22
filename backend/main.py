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

# ─── Router split status ──────────────────────────────────────────────────────
# Phase 1 of splitting this file into backend/routers/*.py is done: points economy
# (shop/redemptions/grants), wipes, notifications, direct messages, reports, polls,
# events & tournaments, and the whole news domain (public/reactions/comments/
# comment-reactions/admin/templates) now live in backend/routers/. Shared helpers used
# across domains moved to backend/helpers.py, the rate limiter to backend/rate_limit.py.
#
# Domains still living in this file, deliberately not split yet (each has real
# cross-domain coupling or was simply out of scope for phase 1 — see the "Split
# backend/main.py into routers" plan for the full rationale):
#   - Auth (register/login/password reset/2FA/session)
#   - Game Plugin Integration (the whole X-Plugin-Key-authenticated surface: status,
#     heartbeat, playtime, connect streak, scheduled restarts, warnings/bans, clan sync)
#   - Bans / Ban appeals / Unified moderation log (coupled via _force_unban, and
#     moderation-log's three-table merge across warnings/bans/appeals)
#   - Clans (game-synced, read-only — coupled to Users via steam_id join)
#   - Monitor / Leaderboard (coupled via the _track_players background task)
#   - The large admin-only tail: Settings (public/admin/import), Users, Linked game
#     accounts, System operations (SSL/update/deploy), Dashboard stats, Comments
#     moderation, File manager, Media library, DB Backup, RCON, Audit log, Analytics
#     (page views), CSV export, Error log, Auto backups list, Bulk user actions
#
# Background tasks (scheduled publish, auto backup, cleanup, monitor poll, scheduler,
# leaderboard snapshot) also stay here — several straddle multiple of the above domains
# (e.g. _track_players writes Leaderboard data from inside what's filed as "Monitor").

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
from .models import Base, User, News, Setting, Comment, Wipe, PlayerRecord, ServerSnapshot, AuditLog, Reaction, PasswordReset, CommentReaction, Notification, Report, Poll, PollOption, PollVote, PageView, ErrorLog, Message, RevokedToken, Event, EventParticipant, PlayerRankSnapshot, PluginHeartbeat, GameClan, GameClanMember, Announcement, ServerMessageTemplate, ServerApiKey, ScheduledRestart, PlayerDailyActivity, PointsTransaction, ShopItem, ShopRedemption
from .rate_limit import limiter
from .helpers import (
    UPLOAD_DIR,
    BACKUP_DIR,
    _totp_pending,
    _visitor_data,
    _explicit_logouts,
    _schedule_restart,
    _cancel_restart,
    _write_maintenance_flag,
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
    _site_timezone,
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
        Setting(key="nav_hidden", value="[]"),
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


from .routers import points_shop, wipes, notifications, messages, reports, polls, events, news, auth as auth_router, profile, clans, leaderboard, plugin_integration, server_admin, users, admin_settings, admin_system, admin_misc, moderation

app.include_router(points_shop.router)
app.include_router(wipes.router)
app.include_router(notifications.router)
app.include_router(messages.router)
app.include_router(reports.router)
app.include_router(polls.router)
app.include_router(events.router)
app.include_router(news.router)
app.include_router(auth_router.router)
app.include_router(profile.router)
app.include_router(clans.router)
app.include_router(leaderboard.router)
app.include_router(plugin_integration.router)
app.include_router(server_admin.router)
app.include_router(users.router)
app.include_router(admin_settings.router)
app.include_router(admin_system.router)
app.include_router(admin_misc.router)
app.include_router(moderation.router)


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


# ─── Who's online ─────────────────────────────────────────────────────────────
# _visitor_data/_explicit_logouts now live in helpers.py (shared with routers/auth.py's
# logout route) — see the comment there.

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


# ─── Discord Webhook ─────────────────────────────────────────────────────────
# _discord_webhook_news (the new-post announce helper) moved to
# backend/routers/news.py — its only caller, POST /api/admin/news, lives there now.

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


