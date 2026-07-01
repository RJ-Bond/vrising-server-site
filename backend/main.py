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
from pathlib import Path

from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException, Request, Query, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, Response
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
from sqlalchemy import select, func, delete, text, or_, update

from .database import engine, get_db
from .models import Base, User, News, Setting, Comment, Wipe, PlayerRecord, ServerSnapshot, AuditLog, Reaction, PasswordReset, Clan, CommentReaction, Notification
from .auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    get_admin_user,
    revoke_token,
    SECRET_KEY,
    ALGORITHM,
    COOKIE_NAME,
    revoked_tokens as _revoked_tokens,
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
    ReactBody,
    ClanCreate,
    ClanUpdate,
    ClanOut,
    ClanDetailOut,
)

OVERSEER_PROMPT = """Ты — Тёмный Управляющий Замком, древний вампирский дух, хранитель этого сервера V Rising.
Твоя задача — помогать игрокам: отвечать на вопросы об игровом сервере, правилах, механиках V Rising, событиях.
Стиль: готический, величественный, слегка таинственный. Обращайся к игрокам как «смертный», «странник» или по имени.
Отвечай на языке вопроса (русский или английский). Максимум 3–4 предложения. Будь полезным и по делу.
Если не знаешь конкретных данных сервера — говори об этом честно, но оставайся в образе."""
from .monitor import get_server_status, get_history

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


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = re.sub(r"^-+|-+$", "", text)
    return text[:200]


async def log_audit(db: AsyncSession, admin: User, action: str, detail: str = "") -> None:
    db.add(AuditLog(admin_username=admin.username, action=action, detail=detail[:512]))


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
    ]
    for s in default_settings:
        existing = await db.execute(select(Setting).where(Setting.key == s.key))
        if existing.scalar_one_or_none() is None:
            db.add(s)
    await db.flush()

    # Если администратор уже существует — считаем настройку завершённой
    admin_result = await db.execute(select(User).where(User.role == "admin").limit(1))
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
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _seed_defaults(db)
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
                init_history(ip, port, [(s.recorded_at.timestamp(), s.players) for s in snaps])
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
    ]
    for slug, updated_at in slugs:
        lastmod = updated_at.strftime("%Y-%m-%d") if updated_at else ""
        urls.append(f"  <url><loc>{base}/news/{slug}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += "\n".join(urls) + "\n</urlset>"
    return Response(content=xml, media_type="application/xml")


# ─── Setup ──────────────────────────────────────────────────────────────────

@app.get("/api/setup/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting).where(Setting.key == "setup_completed"))
    s = result.scalar_one_or_none()
    if s and s.value == "true":
        return {"completed": True}
    admin_result = await db.execute(select(User).where(User.role == "admin").limit(1))
    if admin_result.scalar_one_or_none():
        return {"completed": True}
    return {"completed": False}


@app.post("/api/setup/complete", response_model=TokenOut, status_code=201)
async def setup_complete(body: SetupComplete, response: Response, db: AsyncSession = Depends(get_db)):
    sc_result = await db.execute(select(Setting).where(Setting.key == "setup_completed"))
    sc = sc_result.scalar_one_or_none()
    admin_result = await db.execute(select(User).where(User.role == "admin").limit(1))
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
    token = create_access_token({"sub": str(user.id)})
    _set_auth_cookie(response, token)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@app.post("/api/auth/logout", status_code=204)
async def logout(response: Response, current_user: User = Depends(get_current_user), request: Request = None):
    auth_header = request.headers.get("Authorization", "") if request else ""
    cookie_token = request.cookies.get(COOKIE_NAME, "") if request else ""
    token = auth_header[7:] if auth_header.startswith("Bearer ") else cookie_token
    if token:
        revoke_token(token)
    _clear_auth_cookie(response)


@app.get("/api/auth/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return UserOut.model_validate(current_user)


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
async def change_password(
    body: ChangePasswordBody,
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
    await db.commit()
    return {"ok": True}


@app.post("/api/auth/forgot-password")
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
async def do_reset_password(token: str, body: ResetPasswordBody, db: AsyncSession = Depends(get_db)):
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
    now = datetime.now(timezone.utc)
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    result = await db.execute(
        select(ServerSnapshot)
        .where(ServerSnapshot.server_num == server, ServerSnapshot.recorded_at >= cutoff)
        .order_by(ServerSnapshot.recorded_at.asc())
    )
    snaps = result.scalars().all()
    return [{"ts": int(s.recorded_at.timestamp()), "players": s.players, "online": s.online, "latency_ms": s.latency_ms} for s in snaps]


@app.get("/api/monitor/stats")
async def get_monitor_stats(server: int = Query(1), db: AsyncSession = Depends(get_db)):
    now = datetime.utcnow()
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

    # hourly heatmap: avg players per hour-of-day over last 7 days
    buckets: dict[int, list[int]] = {h: [] for h in range(24)}
    for s in snaps_week:
        buckets[s.recorded_at.hour].append(s.players)
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


@app.get("/api/monitor/status2")
async def server_status2(db: AsyncSession = Depends(get_db)):
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
    total_res = await db.execute(
        select(func.count()).select_from(Comment).where(
            Comment.news_id == news_id, Comment.parent_id == None
        )
    )
    total = total_res.scalar_one()
    pages = max(1, math.ceil(total / per_page))
    result = await db.execute(
        select(Comment)
        .where(Comment.news_id == news_id, Comment.parent_id == None)
        .order_by(Comment.created_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    items = result.scalars().all()
    for c in items:
        rr = await db.execute(
            select(Comment)
            .where(Comment.parent_id == c.id)
            .order_by(Comment.created_at)
            .options(selectinload(Comment.author))
            .limit(50)
        )
        c._replies_list = rr.scalars().all()

    def serialize_comment(c, replies=None):
        return {
            "id": c.id,
            "content": c.content,
            "parent_id": c.parent_id,
            "created_at": c.created_at.isoformat(),
            "author": {"id": c.author.id, "username": c.author.username, "avatar_url": c.author.avatar_url, "role": c.author.role, "is_active": c.author.is_active, "created_at": c.author.created_at.isoformat(), "email": ""} if c.author else None,
            "replies": [serialize_comment(r) for r in (replies or [])],
            "reactions": {},
            "user_reaction": None,
        }

    return {"items": [serialize_comment(c, getattr(c, '_replies_list', [])) for c in items], "total": total, "page": page, "pages": pages}


@app.post("/api/news/{slug}/comments", response_model=CommentOut, status_code=201)
async def add_comment(
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
        if token and token not in _revoked_tokens:
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
    news.updated_at = datetime.now(timezone.utc)
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
    keys = ["site_title", "site_tagline", "site_description", "site_logo_url", "discord_url", "discord_server_id", "bg_image_url", "server_ip", "server_port", "server_name", "server2_name", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2", "event_active", "event_title", "event_text", "event_color", "rules", "timezone", "time_format", "date_format"]
    result = await db.execute(select(Setting).where(Setting.key.in_(keys)))
    settings = result.scalars().all()
    return {s.key: s.value for s in settings}


# ─── Settings (admin) ────────────────────────────────────────────────────────

ALLOWED_SETTING_KEYS = {
    "setup_completed", "server_ip", "server_port", "server_game_port", "server_connect_ip", "server_name",
    "server2_name", "server2_ip", "server2_port", "server2_game_port", "server2_connect_ip",
    "site_title", "site_tagline", "site_description", "site_logo_url", "discord_url", "discord_server_id",
    "bg_image_url", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2",
    "event_active", "event_title", "event_text", "event_color",
    "rules", "https_domain", "https_email",
    "timezone", "time_format", "date_format",
    "rcon_port", "rcon_password", "rcon2_port", "rcon2_password", "discord_webhook_url",
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
        setting.updated_at = datetime.now(timezone.utc)
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
    clan = None
    if user.clan_id:
        clan_result = await db.execute(select(Clan).where(Clan.id == user.clan_id))
        clan_row = clan_result.scalar_one_or_none()
        if clan_row:
            clan = {"id": clan_row.id, "name": clan_row.name, "tag": clan_row.tag}
    return {
        "username": user.username,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "created_at": user.created_at.isoformat(),
        "total_seconds": total_seconds,
        "clan": clan,
    }


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

    out = []
    for r in records:
        item = PlayerRecordOut.model_validate(r)
        item.avatar_url = avatar_map.get(r.player_name)
        out.append(item)
    return out


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


# ─── Clans ────────────────────────────────────────────────────────────────────

async def _clan_out(db: AsyncSession, clan: Clan, with_members: bool = False):
    leader_result = await db.execute(select(User).where(User.id == clan.leader_id))
    leader = leader_result.scalar_one_or_none()
    count_result = await db.execute(select(func.count(User.id)).where(User.clan_id == clan.id))
    member_count = count_result.scalar_one()
    base = {
        "id": clan.id, "name": clan.name, "tag": clan.tag, "description": clan.description or "",
        "leader_id": clan.leader_id, "leader_username": leader.username if leader else "?",
        "member_count": member_count, "created_at": clan.created_at,
    }
    if with_members:
        members_result = await db.execute(select(User).where(User.clan_id == clan.id).order_by(User.username))
        base["members"] = [{"id": m.id, "username": m.username, "avatar_url": m.avatar_url} for m in members_result.scalars().all()]
    return base


@app.get("/api/clans", response_model=list[ClanOut])
async def list_clans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Clan).order_by(Clan.created_at.desc()))
    clans = result.scalars().all()
    return [await _clan_out(db, c) for c in clans]


@app.get("/api/clans/{clan_id}", response_model=ClanDetailOut)
async def get_clan(clan_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Clan).where(Clan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    return await _clan_out(db, clan, with_members=True)


@app.post("/api/clans", response_model=ClanDetailOut, status_code=201)
async def create_clan(
    body: ClanCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.clan_id is not None:
        raise HTTPException(status_code=400, detail="Вы уже состоите в клане — сначала покиньте его")
    existing = await db.execute(
        select(Clan).where(or_(Clan.name == body.name, Clan.tag == body.tag))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Клан с таким названием или тегом уже существует")
    clan = Clan(name=body.name, tag=body.tag, description=body.description, leader_id=current_user.id)
    db.add(clan)
    await db.flush()
    current_user.clan_id = clan.id
    await db.commit()
    await db.refresh(clan)
    return await _clan_out(db, clan, with_members=True)


@app.put("/api/clans/{clan_id}", response_model=ClanDetailOut)
async def update_clan(
    clan_id: int,
    body: ClanUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Clan).where(Clan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    if clan.leader_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только лидер клана может его редактировать")
    clan.description = body.description
    await db.commit()
    await db.refresh(clan)
    return await _clan_out(db, clan, with_members=True)


@app.post("/api/clans/{clan_id}/join", response_model=ClanDetailOut)
async def join_clan(
    clan_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.clan_id is not None:
        raise HTTPException(status_code=400, detail="Вы уже состоите в клане — сначала покиньте его")
    result = await db.execute(select(Clan).where(Clan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    current_user.clan_id = clan.id
    await db.commit()
    await db.refresh(clan)
    return await _clan_out(db, clan, with_members=True)


@app.post("/api/clans/leave")
async def leave_clan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.clan_id is None:
        raise HTTPException(status_code=400, detail="Вы не состоите в клане")
    clan_id = current_user.clan_id
    result = await db.execute(select(Clan).where(Clan.id == clan_id))
    clan = result.scalar_one_or_none()
    current_user.clan_id = None
    if clan and clan.leader_id == current_user.id:
        new_leader_result = await db.execute(
            select(User).where(User.clan_id == clan_id, User.id != current_user.id).order_by(User.created_at).limit(1)
        )
        new_leader = new_leader_result.scalar_one_or_none()
        if new_leader:
            clan.leader_id = new_leader.id
        else:
            await db.delete(clan)
    await db.commit()
    return {"ok": True}


@app.delete("/api/admin/clans/{clan_id}", status_code=204)
async def delete_clan(
    clan_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Clan).where(Clan.id == clan_id))
    clan = result.scalar_one_or_none()
    if clan is None:
        raise HTTPException(status_code=404, detail="Клан не найден")
    await db.execute(
        text("UPDATE users SET clan_id = NULL WHERE clan_id = :cid"), {"cid": clan_id}
    )
    await db.delete(clan)
    await log_audit(db, current_user, "clan.delete", clan.name)
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
async def site_update(_: User = Depends(get_admin_user)):
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
    per_page: int = Query(20, ge=1, le=100),
    q: str = Query(""),
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(Comment.content.ilike(like), User.username.ilike(like), News.title.ilike(like)))
    count_q = select(func.count(Comment.id)).join(News, Comment.news_id == News.id).outerjoin(User, Comment.author_id == User.id).where(*filters)
    total = (await db.execute(count_q)).scalar_one()
    rows = (await db.execute(
        select(Comment, News.title.label("ntitle"), News.slug.label("nslug"),
               User.username.label("uname"))
        .join(News, Comment.news_id == News.id)
        .outerjoin(User, Comment.author_id == User.id)
        .where(*filters)
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
async def download_backup(current_user: User = Depends(get_admin_user)):
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
async def admin_rcon(body: RconBody, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
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
        "items": [
            {"id": r.id, "admin": r.admin_username, "action": r.action,
             "detail": r.detail, "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }
