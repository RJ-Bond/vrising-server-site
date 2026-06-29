import logging
import os
import math
import re
import json
import uuid
import shutil
import time
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Request, status, Query, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, text

from .database import engine, get_db
from .models import Base, User, News, Setting, Comment, Wipe, PlayerRecord, ServerSnapshot, AuditLog, Reaction
from .auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    get_admin_user,
    revoke_token,
    SECRET_KEY,
    ALGORITHM,
    revoked_tokens as _revoked_tokens,
)
from jose import JWTError, jwt as jose_jwt
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
    WipeCreate,
    WipeOut,
    PlayerRecordOut,
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


async def log_audit(db: AsyncSession, admin: User, action: str, detail: str = "") -> None:
    db.add(AuditLog(admin_username=admin.username, action=action, detail=detail[:512]))


async def _seed_defaults(db: AsyncSession):
    default_settings = [
        Setting(key="setup_completed", value="false"),
        Setting(key="server_ip", value=os.getenv("VRISING_SERVER_IP", "127.0.0.1")),
        Setting(key="server_port", value=os.getenv("VRISING_SERVER_PORT", "27016")),
        Setting(key="server_name", value="V Rising Server"),
        Setting(key="site_title", value="V RISING"),
        Setting(key="site_logo_url", value=""),
        Setting(key="discord_url", value=""),
        Setting(key="bg_image_url", value=""),
        Setting(key="server2_name", value=""),
        Setting(key="server2_ip", value=""),
        Setting(key="server2_port", value="27016"),
        Setting(key="discord_server_id", value=""),
        Setting(key="wipe_date", value=""),
        Setting(key="wipe_type", value="full"),
        Setting(key="wipe_date2", value=""),
        Setting(key="wipe_type2", value="full"),
        Setting(key="event_active", value="0"),
        Setting(key="event_title", value=""),
        Setting(key="event_text", value=""),
        Setting(key="event_color", value="crimson"),
        Setting(key="rules", value='[{"icon":"🤝","text":"Уважай других игроков — оскорбления и токсичное поведение запрещены"},{"icon":"🚫","text":"Читы, эксплойты и стороннее ПО — бан без предупреждения"},{"icon":"⚔","text":"Сервер PvE — атаки на других игроков запрещены"},{"icon":"🏰","text":"Запрещено разрушать, красть из построек или гриферить базы других игроков"},{"icon":"🪨","text":"Не перекрывай ресурсные точки и пути прохода своими строениями"},{"icon":"🌱","text":"Помогай новичкам — каждый когда-то начинал с нуля"},{"icon":"🔧","text":"Баги и нарушения сообщай администрации — не используй их в свою пользу"},{"icon":"💬","text":"Спорные ситуации решай через чат или обращайся к администратору"}]'),
    ]
    for s in default_settings:
        existing = await db.execute(select(Setting).where(Setting.key == s.key))
        if existing.scalar_one_or_none() is None:
            db.add(s)
    await db.flush()

    # Если администратор уже существует — считаем настройку завершённой
    admin_result = await db.execute(select(User).where(User.role == "admin"))
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
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _seed_defaults(db)
    yield


limiter = Limiter(key_func=get_remote_address, default_limits=[])

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


# ─── Version ────────────────────────────────────────────────────────────────

@app.get("/api/version")
async def get_version():
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return {"version": version_file.read_text().strip()}
    return {"version": None}


# ─── Sitemap ─────────────────────────────────────────────────────────────────

@app.get("/api/sitemap.xml", response_class=__import__("fastapi.responses", fromlist=["Response"]).Response)
async def sitemap(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(News.slug, News.updated_at).where(News.published == True).order_by(News.updated_at.desc())
    )
    slugs = result.all()
    base = str(request.base_url).rstrip("/")
    urls = [f"  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>"]
    for slug, updated_at in slugs:
        lastmod = updated_at.strftime("%Y-%m-%d") if updated_at else ""
        urls.append(f"  <url><loc>{base}/news/{slug}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += "\n".join(urls) + "\n</urlset>"
    from fastapi.responses import Response
    return Response(content=xml, media_type="application/xml")


# ─── Setup ──────────────────────────────────────────────────────────────────

@app.get("/api/setup/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting).where(Setting.key == "setup_completed"))
    s = result.scalar_one_or_none()
    if s and s.value == "true":
        return {"completed": True}
    admin_result = await db.execute(select(User).where(User.role == "admin"))
    if admin_result.scalar_one_or_none():
        return {"completed": True}
    return {"completed": False}


@app.post("/api/setup/complete", response_model=TokenOut, status_code=201)
async def setup_complete(body: SetupComplete, db: AsyncSession = Depends(get_db)):
    sc_result = await db.execute(select(Setting).where(Setting.key == "setup_completed"))
    sc = sc_result.scalar_one_or_none()
    admin_result = await db.execute(select(User).where(User.role == "admin"))
    if (sc and sc.value == "true") or admin_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Setup already completed")
    existing = await db.execute(select(User).where(
        (User.username == body.username) | (User.email == body.email)
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username or email already taken")
    admin = User(
        username=body.username,
        email=body.email,
        hashed_password=get_password_hash(body.password),
        role="admin",
    )
    db.add(admin)
    await db.flush()
    if sc:
        sc.value = "true"
        sc.updated_at = datetime.utcnow()
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
async def register(request: Request, body: UserRegister, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(
        (User.username == body.username) | (User.email == body.email)
    ))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username or email already taken")
    user = User(
        username=body.username,
        email=body.email,
        hashed_password=get_password_hash(body.password),
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token({"sub": str(user.id)})
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@app.post("/api/auth/login", response_model=TokenOut)
@limiter.limit("10/minute")
async def login(request: Request, body: UserLogin, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Ваш аккаунт был заблокирован.")
    token = create_access_token({"sub": str(user.id)})
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@app.post("/api/auth/logout", status_code=204)
async def logout(current_user: User = Depends(get_current_user), request: Request = None):
    auth_header = request.headers.get("Authorization", "") if request else ""
    if auth_header.startswith("Bearer "):
        revoke_token(auth_header[7:])


@app.get("/api/auth/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return UserOut.model_validate(current_user)


@app.post("/api/auth/change-password")
async def change_password(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    old = (body.get("old_password") or "").strip()
    new = (body.get("new_password") or "").strip()
    if not old or not new:
        raise HTTPException(400, "Заполните все поля")
    if len(new) < 6:
        raise HTTPException(400, "Новый пароль: минимум 6 символов")
    if not verify_password(old, current_user.hashed_password):
        raise HTTPException(400, "Неверный текущий пароль")
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.hashed_password = get_password_hash(new)
    await db.commit()
    return {"ok": True}


@app.post("/api/auth/avatar")
async def upload_avatar(
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


# ─── Monitor ────────────────────────────────────────────────────────────────

async def _track_players(db: AsyncSession, players: list, server_num: int):
    if not players:
        return
    now = datetime.utcnow()
    for p in players:
        name = (p.get("name") or "").strip()
        if not name:
            continue
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
            ))
        else:
            if cur_dur >= rec.last_duration:
                rec.total_seconds += cur_dur - rec.last_duration
            else:
                rec.total_seconds += cur_dur
            rec.last_duration = cur_dur
            rec.last_seen = now
    await db.commit()


_last_snapshot: dict[int, float] = {}
SNAPSHOT_INTERVAL = 300  # 5 minutes


async def _save_snapshot(db: AsyncSession, data: dict, server_num: int):
    now_ts = time.time()
    if now_ts - _last_snapshot.get(server_num, 0) < SNAPSHOT_INTERVAL:
        return
    _last_snapshot[server_num] = now_ts
    snap = ServerSnapshot(
        server_num=server_num,
        recorded_at=datetime.utcnow(),
        online=data.get("online", False),
        players=data.get("players", 0),
        max_players=data.get("max_players", 0),
        latency_ms=data.get("latency_ms"),
        map_name=data.get("map"),
    )
    db.add(snap)
    await db.commit()
    # prune old snapshots (keep 8 days)
    cutoff = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
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
    result = await db.execute(
        select(Setting).where(Setting.key.in_(["server_ip", "server_port", "server_name"]))
    )
    cfg = {s.key: s.value for s in result.scalars().all()}
    ip = cfg.get("server_ip", "127.0.0.1")
    port = int(cfg.get("server_port", "27016"))
    admin_name = cfg.get("server_name", "").strip()
    data = await get_server_status(ip, port)
    if admin_name:
        data = {**data, "name": admin_name}
    elif not data.get("name") or data.get("name") == "Unknown":
        data = {**data, "name": "V Rising Server"}
    await _track_players(db, data.get("players_list", []), 1)
    await _save_snapshot(db, data, 1)
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
async def get_snapshots(server: int = Query(1), db: AsyncSession = Depends(get_db)):
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=7)
    result = await db.execute(
        select(ServerSnapshot)
        .where(ServerSnapshot.server_num == server, ServerSnapshot.recorded_at >= cutoff)
        .order_by(ServerSnapshot.recorded_at.asc())
    )
    snaps = result.scalars().all()
    return [{"ts": int(s.recorded_at.timestamp()), "players": s.players, "online": s.online, "latency_ms": s.latency_ms} for s in snaps]


@app.get("/api/monitor/stats")
async def get_monitor_stats(server: int = Query(1), db: AsyncSession = Depends(get_db)):
    from datetime import timedelta
    now = datetime.utcnow()
    day_ago  = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    res_week = await db.execute(
        select(ServerSnapshot)
        .where(ServerSnapshot.server_num == server, ServerSnapshot.recorded_at >= week_ago)
    )
    snaps_week = res_week.scalars().all()

    res_day = [s for s in snaps_week if s.recorded_at >= day_ago]

    def uptime_pct(snaps):
        if not snaps:
            return None
        return round(sum(1 for s in snaps if s.online) / len(snaps) * 100, 1)

    peak_7d = max((s.players for s in snaps_week), default=0)
    peak_24h = max((s.players for s in res_day), default=0)

    # hourly heatmap: avg players per hour-of-day over last 7 days
    buckets: dict[int, list[int]] = {h: [] for h in range(24)}
    for s in snaps_week:
        buckets[s.recorded_at.hour].append(s.players)
    heatmap = [round(sum(v) / len(v), 1) if v else 0 for _, v in sorted(buckets.items())]

    return {
        "uptime_24h": uptime_pct(res_day),
        "uptime_7d":  uptime_pct(snaps_week),
        "peak_24h":   peak_24h,
        "peak_7d":    peak_7d,
        "heatmap":    heatmap,
    }


@app.get("/api/monitor/status2")
async def server_status2(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Setting).where(Setting.key.in_(["server2_ip", "server2_port", "server2_name"]))
    )
    cfg = {s.key: s.value for s in result.scalars().all()}
    ip = cfg.get("server2_ip", "").strip()
    admin_name = cfg.get("server2_name", "").strip()
    if not ip:
        return {"enabled": False, "online": False, "name": admin_name or "Server 2",
                "players": 0, "max_players": 0, "players_list": []}
    port_str = cfg.get("server2_port", "27016")
    port = int(port_str) if port_str.isdigit() else 27016
    data = await get_server_status(ip, port)
    if admin_name:
        data = {**data, "name": admin_name}
    elif not data.get("name") or data.get("name") == "Unknown":
        data = {**data, "name": "Server 2"}
    await _track_players(db, data.get("players_list", []), 2)
    await _save_snapshot(db, data, 2)
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
async def get_news(slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(News).where(News.slug == slug, News.published == True))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    news.views = (news.views or 0) + 1
    await db.commit()
    await db.refresh(news)
    return NewsOut.model_validate(news)


# ─── Reactions ───────────────────────────────────────────────────────────────

_ALLOWED_EMOJIS = {"fire", "heart", "thumbs_up", "wow"}


@app.get("/api/news/{slug}/reactions")
async def get_reactions(
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
    if authorization and authorization.startswith("Bearer "):
        try:
            token = authorization[7:]
            if token not in _revoked_tokens:
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
async def toggle_reaction(
    slug: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    emoji = body.get("emoji", "")
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

@app.get("/api/news/{slug}/comments", response_model=list[CommentOut])
async def get_comments(slug: str, db: AsyncSession = Depends(get_db)):
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(status_code=404, detail="News not found")
    result = await db.execute(
        select(Comment).where(Comment.news_id == news_id).order_by(Comment.created_at.asc())
    )
    return [CommentOut.model_validate(c) for c in result.scalars().all()]


@app.post("/api/news/{slug}/comments", response_model=CommentOut, status_code=201)
async def add_comment(
    slug: str,
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    news_res = await db.execute(select(News.id).where(News.slug == slug, News.published == True))
    news_id = news_res.scalar_one_or_none()
    if news_id is None:
        raise HTTPException(status_code=404, detail="News not found")
    comment = Comment(news_id=news_id, author_id=current_user.id, content=body.content)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    # eager load author
    await db.refresh(comment, ["author"])
    return CommentOut.model_validate(comment)


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
    if current_user.role != "admin" and comment.author_id != current_user.id:
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
    if current_user.role != "admin" and comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    await db.delete(comment)
    await db.commit()


# ─── Wipes ───────────────────────────────────────────────────────────────────

@app.get("/api/wipes", response_model=list[WipeOut])
async def get_wipes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Wipe).order_by(Wipe.wipe_date.desc()))
    return [WipeOut.model_validate(w) for w in result.scalars().all()]


@app.post("/api/admin/wipes", response_model=WipeOut, status_code=201)
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


@app.delete("/api/admin/wipes/{wipe_id}", status_code=204)
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
    news = News(
        title=body.title,
        slug=slug,
        summary=body.summary,
        content=body.content,
        thumbnail_url=body.thumbnail_url,
        tags=body.tags or "",
        author_id=current_user.id,
        published=body.published,
    )
    db.add(news)
    await log_audit(db, current_user, "news.create", news.title)
    await db.commit()
    await db.refresh(news)
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
    news.updated_at = datetime.utcnow()
    await log_audit(db, admin_u, "news.update", news.title)
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
    await db.delete(news)
    await log_audit(db, admin_u, "news.delete", str(news_id))
    await db.commit()


# ─── File upload ─────────────────────────────────────────────────────────────

_ALLOWED_UPLOAD_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
_ALLOWED_UPLOAD_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/svg+xml",
    "image/webp", "image/x-icon", "image/vnd.microsoft.icon",
}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@app.post("/api/admin/upload")
async def upload_file(
    file: UploadFile = File(...),
    _: User = Depends(get_admin_user),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, detail="Допустимые форматы: PNG, JPG, GIF, SVG, WebP, ICO")
    if file.content_type and file.content_type.split(";")[0].strip() not in _ALLOWED_UPLOAD_MIME:
        raise HTTPException(400, detail="Недопустимый MIME-тип файла")
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(400, detail="Файл слишком большой (максимум 10 МБ)")
    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / filename
    dest.write_bytes(content)
    return {"url": f"/api/uploads/{filename}"}


@app.get("/api/uploads/{filename}")
async def serve_upload(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(404)
    path = UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(str(path))


# ─── Settings (public) ───────────────────────────────────────────────────────

@app.get("/api/settings/public")
async def get_public_settings(db: AsyncSession = Depends(get_db)):
    keys = ["site_title", "site_logo_url", "discord_url", "discord_server_id", "bg_image_url", "server_ip", "server_port", "server_name", "server2_name", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2", "event_active", "event_title", "event_text", "event_color", "rules"]
    result = await db.execute(select(Setting).where(Setting.key.in_(keys)))
    settings = result.scalars().all()
    return {s.key: s.value for s in settings}


# ─── Settings (admin) ────────────────────────────────────────────────────────

ALLOWED_SETTING_KEYS = {
    "setup_completed", "server_ip", "server_port", "server_name",
    "server2_name", "server2_ip", "server2_port",
    "site_title", "site_logo_url", "discord_url", "discord_server_id",
    "bg_image_url", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2",
    "event_active", "event_title", "event_text", "event_color",
    "rules", "https_domain", "https_email",
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
    _: User = Depends(get_admin_user),
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
        setting.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(setting)
    return SettingOut.model_validate(setting)


# ─── Users (admin) ───────────────────────────────────────────────────────────

@app.get("/api/admin/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@app.put("/api/admin/users/{user_id}/role")
async def change_role(
    user_id: int,
    role: str = Query(..., regex="^(user|admin)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
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
    current_user: User = Depends(get_admin_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = not user.is_active
    await log_audit(db, current_user, "user.toggle", user.username)
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
    await db.delete(user)
    await log_audit(db, current_user, "user.delete", user.username)
    await db.commit()


# ─── Public profile ──────────────────────────────────────────────────────────

@app.get("/api/users/{username}")
async def get_public_profile(username: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.username == username, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    total_result = await db.execute(
        select(func.sum(PlayerRecord.total_seconds)).where(PlayerRecord.player_name == username)
    )
    total_seconds = total_result.scalar_one() or 0
    return {
        "username": user.username,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "created_at": user.created_at.isoformat(),
        "total_seconds": total_seconds,
    }


# ─── Leaderboard ─────────────────────────────────────────────────────────────

@app.get("/api/leaderboard", response_model=list[PlayerRecordOut])
async def get_leaderboard(
    server: int = Query(1),
    period: str = Query("all"),
    db: AsyncSession = Depends(get_db),
):
    q = select(PlayerRecord).where(PlayerRecord.server_num == server)
    if period == "week":
        cutoff = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        cutoff -= timedelta(days=7)
        q = q.where(PlayerRecord.last_seen >= cutoff)
    q = q.order_by(PlayerRecord.total_seconds.desc()).limit(20)
    result = await db.execute(q)
    return [PlayerRecordOut.model_validate(r) for r in result.scalars().all()]


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
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Setting).where(Setting.key.in_(["https_domain", "https_email"]))
    )
    smap = {s.key: s.value for s in result.scalars()}
    domain = smap.get("https_domain", "").strip()
    email = smap.get("https_email", "").strip()
    if not domain or not email:
        raise HTTPException(400, "Заполните домен и email в настройках HTTPS")

    async def stream():
        def sse(msg: str) -> str:
            return f"data: {msg}\n\n"

        yield sse(f"🔐 Запрашиваем сертификат Let's Encrypt для {domain}...")

        rc = 0
        async for line in _stream_cmd(
            "docker", "run", "--rm",
            "-v", "vrising_letsencrypt:/etc/letsencrypt",
            "-v", "vrising_certbot_webroot:/var/www/certbot",
            "certbot/certbot",
            "certonly", "--webroot",
            "--webroot-path=/var/www/certbot",
            "-d", domain,
            "--email", email,
            "--agree-tos", "--non-interactive", "--no-eff-email",
        ):
            if line.startswith("__rc__"):
                rc = int(line[6:])
            else:
                yield sse(line)

        if rc != 0:
            yield sse("❌ Ошибка получения сертификата. Проверьте что A-запись домена указывает на этот сервер.")
            yield sse("DONE:error")
            return

        yield sse("📝 Обновляем конфигурацию nginx...")
        try:
            workspace = "/workspace"
            with open(f"{workspace}/nginx/nginx-ssl.conf") as f:
                ssl_conf = f.read().replace("DOMAIN", domain)
            with open(f"{workspace}/nginx/nginx.conf", "w") as f:
                f.write(ssl_conf)
            yield sse(f"✅ nginx.conf обновлён для домена {domain}")
        except Exception as exc:
            yield sse(f"❌ Ошибка записи конфига: {exc}")
            yield sse("DONE:error")
            return

        yield sse("🔄 Перезапускаем nginx...")
        rc2 = 0
        async for line in _stream_cmd("docker", "exec", "vrising_nginx", "nginx", "-s", "reload"):
            if line.startswith("__rc__"):
                rc2 = int(line[6:])
            else:
                yield sse(line)

        if rc2 != 0:
            async for line in _stream_cmd("docker", "restart", "vrising_nginx"):
                if not line.startswith("__rc__"):
                    yield sse(line)

        yield sse("🎉 HTTPS успешно настроен! Сайт теперь доступен по https://" + domain)
        yield sse("DONE:ok")

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/admin/update")
async def site_update(_: User = Depends(get_admin_user)):
    async def stream():
        def sse(msg: str) -> str:
            return f"data: {msg}\n\n"

        yield sse("📦 Получаем обновления из репозитория...")

        rc = 0
        async for line in _stream_cmd("git", "-C", "/workspace", "pull", "--ff-only"):
            if line.startswith("__rc__"):
                rc = int(line[6:])
            else:
                yield sse(line)

        if rc != 0:
            yield sse("❌ Ошибка git pull. Убедитесь что репозиторий настроен и нет конфликтов.")
            yield sse("DONE:error")
            return

        yield sse("✅ Код обновлён. Frontend применён мгновенно.")
        yield sse("🔄 Backend перезагружается автоматически через uvicorn --reload...")
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
    per_page: int = Query(20, ge=1, le=100),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(Comment.id)))).scalar_one()
    rows = (await db.execute(
        select(Comment, News.title.label("ntitle"), News.slug.label("nslug"),
               User.username.label("uname"))
        .join(News, Comment.news_id == News.id)
        .outerjoin(User, Comment.author_id == User.id)
        .order_by(Comment.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).all()
    return {
        "total": total,
        "items": [
            {"id": r.Comment.id, "content": r.Comment.content,
             "news_title": r.ntitle, "news_slug": r.nslug,
             "author": r.uname or "Аноним",
             "created_at": r.Comment.created_at.isoformat()}
            for r in rows
        ],
    }


# ─── File manager ────────────────────────────────────────────────────────────

@app.get("/api/admin/uploads")
async def list_uploads(_: User = Depends(get_admin_user)):
    files = []
    if UPLOAD_DIR.exists():
        for f in sorted(UPLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file():
                st = f.stat()
                files.append({
                    "filename": f.name,
                    "url": f"/api/uploads/{f.name}",
                    "size": st.st_size,
                    "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
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
            s.updated_at = datetime.utcnow()
        else:
            db.add(Setting(key=key, value=str(value)))
        count += 1
    await db.commit()
    return {"imported": count}


# ─── Audit log ───────────────────────────────────────────────────────────────

@app.get("/api/admin/audit-log")
async def get_audit_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(AuditLog.id)))).scalar_one()
    rows = (await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {
        "total": total,
        "items": [
            {"id": r.id, "admin": r.admin_username, "action": r.action,
             "detail": r.detail, "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }
