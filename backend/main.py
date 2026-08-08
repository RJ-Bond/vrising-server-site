import html
import logging
import os
import re
import json
import sys
import time
import asyncio
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# ─── Router split status ──────────────────────────────────────────────────────
# The split into backend/routers/*.py went further than this comment used to claim —
# it was last updated after phase 1 and never revised as later phases landed. Current
# state (verified against the actual app.include_router() calls below, not this
# comment — trust those first if the two ever disagree again): auth, profile, users,
# clans, leaderboard, news (+reactions/comments/polls/templates), wipes, events,
# notifications, direct messages, reports, points economy (shop/redemptions/grants),
# bans/ban-appeals/unified moderation-log, game plugin integration (the whole
# X-Plugin-Key surface), server admin (restarts/message-templates/server-api-key),
# and the admin settings/system/misc tail (users, files, media, backups, RCON, audit
# log, analytics, CSV export, error log) are ALL split out now. Shared helpers moved
# to backend/helpers.py, the rate limiter to backend/rate_limit.py.
#
# What's still genuinely here, not yet split (see the "Split backend/main.py into
# routers" plan for rationale — mostly background-task coupling, not scope creep):
#   - Version / SEO (sitemap.xml, rss.xml, news-embed prerender, Google verification)
#   - Setup wizard (first-run only)
#   - Presence ("who's online" ping/stream) and server Monitor (A2S polling, history/
#     snapshots) — coupled via the _track_players background task, which also feeds
#     Leaderboard
#   - Admin chat announcements (CRUD + send-now/test-send) and admin plugin-status
#   - Background tasks themselves: scheduled news publish, auto-backup, cleanup,
#     leaderboard rank snapshots, event status transitions
#
# Background tasks (scheduled publish, auto backup, cleanup, monitor poll, scheduler,
# leaderboard snapshot) also stay here — several straddle multiple of the above domains
# (e.g. _track_players writes Leaderboard data from inside what's filed as "Monitor").

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, func, text
from sqlalchemy.orm import selectinload

from .database import engine, get_db
from .models import User, News, Setting, PlayerRecord, ServerSnapshot, PageView, ErrorLog, RevokedToken, Event, PlayerRankSnapshot, PluginHeartbeat, Announcement, GameClan, GameClanMember, PushSubscription
from .rate_limit import limiter
from .helpers import (
    BACKUP_DIR,
    copy_backup_offsite,
    _visitor_data,
    _explicit_logouts,
    _write_maintenance_flag,
    _fmt_dt,
    _utc_ts,
    _set_auth_cookie,
    _audit,
    activity_broadcast,
    send_newsletter_digest,
    send_push,
)
from .auth import (
    get_password_hash,
    create_access_token,
    get_admin_user,
    get_optional_user,
)
from .schemas import (
    UserOut,
    TokenOut,
    SetupComplete,
    PluginHeartbeatOut,
    AnnouncementCreate,
    AnnouncementUpdate,
    AnnouncementOut,
    AnnouncementTestSend,
)
from .monitor import get_server_status, get_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Optional Sentry error monitoring ──────────────────────────────────────
# Complete no-op unless SENTRY_DSN is set (docker-compose.yml / .env.example) — safe
# to ship even though nobody has a Sentry project yet. sentry_sdk is imported
# unconditionally up top (cheap even unused); only the .init() call below is gated
# behind the env var. The FastAPI integration auto-instruments every route (including
# the routers/*.py split) so unhandled 500s get reported without wrapping each handler
# by hand. Sample rate is kept low/configurable — this is a small hobby-scale site,
# not a candidate for full trace sampling.
_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    # environment/release let Sentry group and filter issues by deploy — without them
    # every error from every environment/version lands in one undifferentiated stream,
    # so there's no way to tell "still happening on the latest deploy" from "already
    # fixed two releases ago". release reads the same live VERSION file GET /api/version
    # already serves (bumped on every release, see that endpoint's own comment).
    _version_file = Path("/opt/vrising-site/VERSION")
    _release = _version_file.read_text().strip() if _version_file.exists() else None
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[FastApiIntegration()],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        environment=os.getenv("ENVIRONMENT", "production"),
        release=_release,
    )
    logger.info("Sentry error monitoring enabled")

# Repo root is bind-mounted read/write at /opt/vrising-site (see docker-compose.yml) for
# the deploy/update endpoints; reused here to serve frontend/index.html for news-embed.
_INDEX_HTML_PATH = "/opt/vrising-site/frontend/index.html"

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
        # Homepage content — all optional, empty by default so a fresh install's
        # homepage doesn't show fabricated placeholder testimonials/screenshots. See
        # frontend/index.html's rendering of GET /api/settings/public's home_* keys.
        Setting(key="home_pitch", value=""),
        Setting(key="home_testimonials", value="[]"),  # [{"author":"...","text":"..."}]
        Setting(key="home_gallery", value="[]"),  # ["https://.../img1.jpg", ...]
        Setting(key="site_launched_date", value=""),  # "YYYY-MM-DD", optional
        # 2-3 short newline-separated bullets shown above the full rules accordion on the
        # homepage (frontend/index.js's loadSiteSettings()/toggleRules()) — a fast summary
        # before the expandable detail. Rules content itself (the "rules" key) is free text
        # an admin writes per-rule, so this can't be derived algorithmically; empty by
        # default, same "hidden until configured" convention as home_pitch above.
        Setting(key="rules_tldr", value=""),
        # Points economy — earning rates, tunable by an admin on the Economy tab
        # (not exposed on /api/settings/public: admin-only tuning, no anonymous use).
        Setting(key="points_per_minute_playtime", value="1"),
        Setting(key="points_streak_bonus", value="10"),
        Setting(key="points_streak_min_days", value="2"),
        # Points economy — spend side, the plugin's ".nick <name>" chat command.
        Setting(key="nickname_change_cost", value="100"),
        Setting(key="nickname_change_cooldown_days", value="7"),
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


async def _run_db_migrations() -> None:
    """Bring the DB schema up to date via Alembic (alembic/versions/) instead of the
    old Base.metadata.create_all() + growing hand-written ALTER TABLE list this used to
    be (see git history / CLAUDE.md). Delegates the actual decision logic to
    backend/db_migrate.py — see its module docstring for why a plain `alembic upgrade
    head` isn't safe on its own (existing, pre-Alembic production DBs need a one-time
    `stamp head` adoption first) and why this shells out to a *subprocess* rather than
    calling Alembic's Python API in-process: alembic/env.py calls fileConfig() on
    alembic.ini, which would reconfigure (and likely disable) this app's own logging
    setup above if run inside this same process.

    Raises on failure — a broken/partial schema is not something to silently continue
    booting against; docker-compose's `restart: unless-stopped` + healthcheck make a
    crash-loop here loud (visible in `docker compose logs`) rather than a silent
    half-working deploy.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "backend.db_migrate",
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    output = out.decode(errors="replace").strip()
    if output:
        logger.info("db_migrate: %s", output)
    if proc.returncode != 0:
        raise RuntimeError(f"Database migration failed (exit {proc.returncode}) — refusing to start:\n{output}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _run_db_migrations()
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
    task_newsletter = asyncio.create_task(_newsletter_digest_task())
    yield
    task_publish.cancel()
    task_backup.cancel()
    task_cleanup.cancel()
    task_monitor.cancel()
    task_scheduler.cancel()
    task_ranksnap.cancel()
    task_newsletter.cancel()


app = FastAPI(title="V Rising Server Site", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Applies rate_limit.py's default_limits to every route that has no @limiter.limit of
# its own — without this middleware, default_limits is inert and only the handful of
# routes with an explicit decorator were ever actually rate-limited (most routers had
# none at all: admin_misc, admin_settings, admin_system, clans, events, leaderboard,
# notifications, points_shop, server_admin, users, wipes).
app.add_middleware(SlowAPIMiddleware)

_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
_origins = [o.strip() for o in _raw_origins.split(",")] if _raw_origins != "*" else ["*"]
# allow_origins=["*"] + allow_credentials=True is a combination no real browser will
# ever actually honor (the CORS spec forbids a wildcard origin alongside credentials),
# so silently starting in this state just means every cross-origin request quietly
# fails for real clients while looking "configured" — fail fast instead, same as
# SECRET_KEY above. docker-compose.yml already sets a real ALLOWED_ORIGINS default;
# this only bites a bare `uvicorn backend.main:app` run with no .env loaded.
if _origins == ["*"]:
    raise RuntimeError(
        "ALLOWED_ORIGINS не задан. Укажите список разрешённых origin через запятую в .env "
        "(ALLOWED_ORIGINS is unset — set a comma-separated origin list in .env; "
        "wildcard '*' can't be combined with credentialed requests)."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


import hashlib  # noqa: E402 — kept next to the middleware that's its only user, not worth a top-of-file import for one call site
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402 — same reason


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


from .routers import points_shop, wipes, notifications, messages, reports, polls, events, news, auth as auth_router, profile, clans, leaderboard, plugin_integration, server_admin, users, admin_settings, admin_system, admin_misc, moderation, search, activity_feed  # noqa: E402 — deliberately after `app`/middleware are fully set up, right above the include_router() calls that use it

app.include_router(points_shop.router)
app.include_router(search.router)
app.include_router(activity_feed.router)
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


# ─── Health check ────────────────────────────────────────────────────────────
# Unauthenticated liveness/readiness probe (uptime monitors, container orchestration).
# Does a trivial real query rather than just returning a static 200 so it actually
# reflects DB reachability, not merely "the process is alive" — deliberately cheap
# (no ORM, no table scan) so it stays safe to poll frequently.

@app.get("/healthz")
async def healthz(db: AsyncSession = Depends(get_db)):
    await db.execute(text("SELECT 1"))
    return {"status": "ok"}


# ─── Homepage trust stats ───────────────────────────────────────────────────
# Backs the homepage's "sколько всего игроков"/"часов наиграно"/"топ клан" blocks —
# all real, computed from actual data (no fabricated numbers): registered accounts,
# summed PlayerRecord.total_seconds (same source servers.html/leaderboard already
# use), and whichever GameClan currently has the most members (game-synced roster,
# not the old unused Clan model — see GameClan's own docstring).

@app.get("/api/homepage-stats")
async def get_homepage_stats(db: AsyncSession = Depends(get_db)):
    total_users = (await db.execute(select(func.count(User.id)).where(User.is_active == True))).scalar_one()
    total_seconds = (await db.execute(select(func.sum(PlayerRecord.total_seconds)))).scalar_one() or 0

    top_clan = None
    member_counts = (await db.execute(
        select(GameClanMember.clan_id, func.count(GameClanMember.id).label("cnt"))
        .group_by(GameClanMember.clan_id).order_by(func.count(GameClanMember.id).desc()).limit(1)
    )).first()
    if member_counts:
        clan = (await db.execute(select(GameClan).where(GameClan.id == member_counts.clan_id))).scalar_one_or_none()
        if clan:
            top_clan = {"name": clan.name, "member_count": member_counts.cnt}

    return {
        "total_users": total_users,
        "total_hours": round(total_seconds / 3600),
        "top_clan": top_clan,
    }


# ─── Version ────────────────────────────────────────────────────────────────

@app.get("/api/version")
async def get_version():
    """Reads the SAME path as admin_system.py's _REPO_VERSION_FILE (keep these two in
    sync) — the git checkout bind-mounted at /opt/vrising-site (docker-compose.yml's
    ".:/opt/vrising-site" volume), not /app/VERSION. /app/VERSION is baked into the
    Docker image at build time (Dockerfile's "COPY VERSION ./") and NOT bind-mounted,
    so it goes stale after every routine deploy (git pull + uvicorn --reload, no image
    rebuild) — this was previously the site — the public footer showed an old version
    while the admin panel's update-check (already on the live-mounted path) correctly
    saw the new one."""
    version_file = Path("/opt/vrising-site/VERSION")
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

    # Events merged into the same feed alongside news (not a separate block) — reasonable
    # choice: only "upcoming"/"active" events (an "ended"/"cancelled" event is stale news
    # a feed reader gains nothing from), newest-announced first. created_at (when the
    # event was added, same field news sorts on) is used as the item's pubDate/sort key
    # rather than start_date — pubDate conventionally means "when this was published to
    # the feed", not "when the thing described happens"; the actual start time is folded
    # into the description text instead so readers still see it.
    event_result = await db.execute(
        select(Event)
        .where(Event.status.in_(("upcoming", "active")))
        .order_by(Event.created_at.desc())
        .limit(20)
    )
    events = event_result.scalars().all()

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

    def _xml_escape(text: str) -> str:
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _rfc822(dt):
        if dt is None:
            return "", datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000"), dt

    # (sort_key, rendered <item>) pairs, merged and sorted below so events interleave
    # with news in one reverse-chronological list instead of appearing as a separate block.
    feed_entries = []

    for n in news_items:
        title = _xml_escape(n.title)
        desc = _xml_escape(_strip_html.sub("", n.content or "")[:300])
        link = f"{base_url}/?news={n.slug}"
        pub_date, sort_key = _rfc822(n.created_at)
        feed_entries.append((sort_key, f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <description>{desc}</description>
      <pubDate>{pub_date}</pubDate>
      <guid>{link}</guid>
    </item>"""))

    for ev in events:
        title = _xml_escape(ev.title)
        when = _fmt_dt(ev.start_date) or ""
        body = _strip_html.sub("", ev.description or "")[:250]
        desc = _xml_escape(f"{when} — {body}" if body else when)
        # events.html reads ?event=<id> to scroll to/highlight the matching card
        # (see _scrollToDeepLinkedEvent in events.html).
        link = f"{base_url}/events.html?event={ev.id}"
        pub_date, sort_key = _rfc822(ev.created_at)
        feed_entries.append((sort_key, f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <description>{desc}</description>
      <pubDate>{pub_date}</pubDate>
      <guid>{link}</guid>
    </item>"""))

    feed_entries.sort(key=lambda entry: entry[0], reverse=True)
    items_xml = "".join(entry[1] for entry in feed_entries)

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
    except OSError as e:
        raise HTTPException(status_code=404, detail="index.html not found") from e

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

# Previous online/offline state per server_num, tracked only by the periodic
# _monitor_poll_cycle() (not the on-demand /status endpoints) so "back online"
# push notifications fire on a real offline->online transition and never on
# every poll while a server stays up. None means "unknown" — seeded that way
# so the first poll after a process restart never fires a false transition.
_prev_server_online: dict[int, Optional[bool]] = {}


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


# POST /api/admin/test-webhook lives in backend/routers/admin_misc.py — it used to be
# duplicated here (identical path + function name, both registered), which threw a
# "Duplicate Operation ID" warning on every startup/test run. Removed in favor of the
# admin_misc.py copy, the intended home per the router split (see that file's own
# Discord Webhook section header comment).


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
                    for n in rows:
                        activity_broadcast({
                            "type": "news", "title": n.title, "subtitle": (n.summary or "").strip(),
                            "url": f"/?news={n.slug}", "icon": "📰", "timestamp": _fmt_dt(n.created_at),
                        })
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
                    for news_item in items:
                        activity_broadcast({
                            "type": "news", "title": news_item.title, "subtitle": (news_item.summary or "").strip(),
                            "url": f"/?news={news_item.slug}", "icon": "📰", "timestamp": _fmt_dt(news_item.created_at),
                        })

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
    """Create daily DB backup at midnight UTC, then (if BACKUP_REMOTE_PATH is set) copy
    it offsite too — see copy_backup_offsite()'s docstring in helpers.py for why that's a
    plain local/mounted-path copy rather than a specific cloud API, and how a maintainer
    points it at a real destination."""
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
                # copy_backup_offsite shells out to rsync (if present) / does blocking
                # file I/O via shutil — offload to a thread so a slow/unmounted remote
                # path can't stall this task's event loop.
                await asyncio.to_thread(copy_backup_offsite, dst)
                # keep last 7 backups (local only — BACKUP_REMOTE_PATH is expected to
                # manage its own retention; this app has no way to know what else lives
                # at that destination or what retention policy it should follow there)
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


async def _newsletter_digest_task():
    """Weekly: email opted-in users a digest of news published since the last send.
    Fires every Monday at 09:00 UTC. What actually gets sent (the "since last send"
    cutoff) is tracked in the "newsletter_last_sent_at" Setting by
    send_newsletter_digest() itself, not by this loop's timing — so a missed or
    delayed run (deploy downtime spanning the trigger time, etc.) still only sends
    what's genuinely new next time it fires, same shape as the other *_task loops
    in this module (_auto_backup_task, _leaderboard_snapshot_task)."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            days_until_monday = (7 - now.weekday()) % 7
            next_run = (now + timedelta(days=days_until_monday)).replace(hour=9, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=7)
            await asyncio.sleep((next_run - now).total_seconds())
            async with AsyncSession(engine, expire_on_commit=False) as db:
                summary = await send_newsletter_digest(db)
                logger.info(
                    "Newsletter digest: items=%d recipients=%d sent=%d",
                    summary["items"], summary["recipients"], summary["sent"],
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("_newsletter_digest_task error: %s", e)


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

        # Web Push "favorite server back online" trigger — fires only on an
        # actual offline->online transition (never on every poll while a
        # server stays up, and never on the first poll after a process
        # restart, where the previous state is unknown/None).
        now_online = bool(data.get("online"))
        prev_online = _prev_server_online.get(server_num)
        if prev_online is False and now_online:
            display_name = admin_name or data.get("name") or f"Server {server_num}"
            asyncio.create_task(_notify_server_back_online(display_name))
        _prev_server_online[server_num] = now_online


async def _notify_server_back_online(server_name: str) -> None:
    """Fan out a push notification to every user with at least one PushSubscription
    row when a monitored game server transitions from offline to online. There's no
    per-server subscription preference today, so this notifies everyone subscribed to
    push at all (same coarse granularity as the news-push trigger). Fire-and-forget:
    callers schedule this via asyncio.create_task, same pattern as every other
    send_push() call site, so a slow/unreachable push service never blocks the
    monitor poll loop."""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        res = await db.execute(select(PushSubscription.user_id).distinct())
        user_ids = [row[0] for row in res.all()]
    for user_id in user_ids:
        asyncio.create_task(send_push(
            user_id,
            "Сервер снова в сети",
            f"{server_name} снова онлайн",
            "/servers.html",
        ))


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


