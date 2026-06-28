import os
import math
import re
import json
import uuid
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, status, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, text

from .database import engine, get_db
from .models import Base, User, News, Setting, Comment, Wipe, PlayerRecord
from .auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    get_admin_user,
)
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
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await _seed_defaults(db)
    yield


app = FastAPI(title="V Rising Server Site", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Version ────────────────────────────────────────────────────────────────

@app.get("/api/version")
async def get_version():
    version_file = Path("/app/VERSION")
    if version_file.exists():
        return {"version": version_file.read_text().strip()}
    return {"version": None}


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
async def castle_overseer_chat(body: ChatRequest):
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
async def register(body: UserRegister, db: AsyncSession = Depends(get_db)):
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
async def login(body: UserLogin, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    token = create_access_token({"sub": str(user.id)})
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


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
    db: AsyncSession = Depends(get_db),
):
    base_filter = News.published == True
    if tag:
        base_filter = base_filter & News.tags.contains(tag)

    total_result = await db.execute(
        select(func.count()).select_from(News).where(base_filter)
    )
    total = total_result.scalar_one()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    result = await db.execute(
        select(News)
        .where(base_filter)
        .order_by(News.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    items = result.scalars().all()
    return PaginatedNews(
        items=[NewsListOut.model_validate(n) for n in items],
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
    return NewsOut.model_validate(news)


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
    await db.commit()
    await db.refresh(news)
    result = await db.execute(select(News).where(News.id == news.id))
    return NewsOut.model_validate(result.scalar_one())


@app.put("/api/admin/news/{news_id}", response_model=NewsOut)
async def update_news(
    news_id: int,
    body: NewsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(News).where(News.id == news_id))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    if body.title is not None:
        news.title = body.title
    if body.summary is not None:
        news.summary = body.summary
    if body.content is not None:
        news.content = body.content
    if body.thumbnail_url is not None:
        news.thumbnail_url = body.thumbnail_url
    if body.tags is not None:
        news.tags = body.tags
    if body.published is not None:
        news.published = body.published
    news.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(news)
    result2 = await db.execute(select(News).where(News.id == news.id))
    return NewsOut.model_validate(result2.scalar_one())


@app.delete("/api/admin/news/{news_id}", status_code=204)
async def delete_news(
    news_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    result = await db.execute(select(News).where(News.id == news_id))
    news = result.scalar_one_or_none()
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    await db.delete(news)
    await db.commit()


# ─── File upload ─────────────────────────────────────────────────────────────

@app.post("/api/admin/upload")
async def upload_file(
    file: UploadFile = File(...),
    _: User = Depends(get_admin_user),
):
    allowed = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, detail="Допустимые форматы: PNG, JPG, GIF, SVG, WebP, ICO")
    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
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
    keys = ["site_title", "site_logo_url", "discord_url", "discord_server_id", "bg_image_url", "server_ip", "server_port", "server_name", "server2_name", "wipe_date", "wipe_type", "wipe_date2", "wipe_type2"]
    result = await db.execute(select(Setting).where(Setting.key.in_(keys)))
    settings = result.scalars().all()
    return {s.key: s.value for s in settings}


# ─── Settings (admin) ────────────────────────────────────────────────────────

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
    await db.commit()


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
