import os
import math
import re
import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from .database import engine, get_db
from .models import Base, User, News, Setting
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
)

OVERSEER_PROMPT = """Ты — Тёмный Управляющий Замком, древний вампирский дух, хранитель этого сервера V Rising.
Твоя задача — помогать игрокам: отвечать на вопросы об игровом сервере, правилах, механиках V Rising, событиях.
Стиль: готический, величественный, слегка таинственный. Обращайся к игрокам как «смертный», «странник» или по имени.
Отвечай на языке вопроса (русский или английский). Максимум 3–4 предложения. Будь полезным и по делу.
Если не знаешь конкретных данных сервера — говори об этом честно, но оставайся в образе."""
from .monitor import get_server_status


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


# ─── Monitor ────────────────────────────────────────────────────────────────

@app.get("/api/monitor/status")
async def server_status(db: AsyncSession = Depends(get_db)):
    ip_row = await db.execute(select(Setting).where(Setting.key == "server_ip"))
    port_row = await db.execute(select(Setting).where(Setting.key == "server_port"))
    ip_setting = ip_row.scalar_one_or_none()
    port_setting = port_row.scalar_one_or_none()
    ip = ip_setting.value if ip_setting else "127.0.0.1"
    port = int(port_setting.value) if port_setting else 27016
    return await get_server_status(ip, port)


# ─── News (public) ──────────────────────────────────────────────────────────

@app.get("/api/news", response_model=PaginatedNews)
async def list_news(
    page: int = Query(1, ge=1),
    per_page: int = Query(5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    total_result = await db.execute(
        select(func.count()).select_from(News).where(News.published == True)
    )
    total = total_result.scalar_one()
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    result = await db.execute(
        select(News)
        .where(News.published == True)
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


# ─── Settings (public) ───────────────────────────────────────────────────────

@app.get("/api/settings/public")
async def get_public_settings(db: AsyncSession = Depends(get_db)):
    keys = ["site_title", "site_logo_url"]
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
