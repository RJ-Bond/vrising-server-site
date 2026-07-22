import asyncio
import os
import struct
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import User, Setting, News
from ..auth import get_admin_user, get_superadmin_user
from ..helpers import UPLOAD_DIR, BACKUP_DIR, log_audit

router = APIRouter()


# ─── File upload ─────────────────────────────────────────────────────────────

_ALLOWED_UPLOAD_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
_ALLOWED_UPLOAD_MIME = {
    "image/png", "image/jpeg", "image/gif",
    "image/webp", "image/x-icon", "image/vnd.microsoft.icon",
}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/api/admin/upload")
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


@router.get("/api/uploads/covers/{filename}")
async def serve_cover_upload(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(404)
    path = UPLOAD_DIR / "covers" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/api/uploads/{filename}")
async def serve_upload(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(404)
    path = UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=31536000, immutable"})


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


@router.post("/api/admin/ssl/install")
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


@router.post("/api/admin/update")
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


# ─── File manager ────────────────────────────────────────────────────────────

@router.get("/api/admin/uploads")
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


@router.delete("/api/admin/uploads/{filename}", status_code=204)
async def delete_upload(filename: str, _: User = Depends(get_admin_user)):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")
    path.unlink()


# ─── Media library ───────────────────────────────────────────────────────────

@router.get("/api/admin/media")
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


@router.delete("/api/admin/media/{filename:path}", status_code=200)
async def delete_media(filename: str, _: User = Depends(get_admin_user)):
    if ".." in filename or os.path.isabs(filename):
        raise HTTPException(400, "Invalid filename")
    full_path = UPLOAD_DIR / filename
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(404, "File not found")
    full_path.unlink()
    return {"ok": True}


# ─── DB Backup ───────────────────────────────────────────────────────────────

@router.get("/api/admin/backup")
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


@router.post("/api/admin/rcon")
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


# ─── Auto backups list ────────────────────────────────────────────────────────

@router.get("/api/admin/backups")
async def list_backups(_: User = Depends(get_superadmin_user)):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("vrising_*.db"), reverse=True)
    return [
        {"filename": f.name, "size": f.stat().st_size,
         "created_at": datetime.utcfromtimestamp(f.stat().st_mtime).isoformat()}
        for f in files
    ]


@router.get("/api/admin/backups/{filename}")
async def download_named_backup(filename: str, _: User = Depends(get_superadmin_user)):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = BACKUP_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Backup not found")
    return FileResponse(path=str(path), media_type="application/octet-stream", filename=filename)


@router.post("/api/admin/backups/create", status_code=201)
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
