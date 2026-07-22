import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, text

from ..database import get_db
from ..models import User, PasswordReset
from ..auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    get_current_user,
    get_admin_user,
    revoke_token,
    COOKIE_NAME,
)
from ..rate_limit import limiter
from ..helpers import (
    UPLOAD_DIR,
    _totp_pending,
    _visitor_data,
    _explicit_logouts,
    _set_auth_cookie,
    _clear_auth_cookie,
    _send_reset_email,
)
from ..schemas import (
    UserRegister,
    UserLogin,
    UserOut,
    TokenOut,
    ForgotPasswordRequest,
    ResetPasswordBody,
    ChangePasswordBody,
    ChangeEmailBody,
)

router = APIRouter()


# ─── Auth ───────────────────────────────────────────────────────────────────

@router.post("/api/auth/register", response_model=TokenOut, status_code=201)
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


@router.post("/api/auth/login", response_model=TokenOut)
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


@router.post("/api/auth/logout", status_code=204)
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


@router.get("/api/auth/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    last = current_user.last_active_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if not last or (now - last).total_seconds() > 60:
        current_user.last_active_at = now
        await db.commit()
    return UserOut.model_validate(current_user)


@router.post("/api/auth/accept-rules", response_model=UserOut)
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


@router.post("/api/auth/change-password")
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


@router.post("/api/auth/change-email")
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


@router.get("/api/auth/2fa/setup")
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


@router.post("/api/auth/2fa/enable")
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


@router.post("/api/auth/2fa/disable")
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


@router.post("/api/auth/forgot-password")
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


@router.get("/api/auth/reset-password/{token}")
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


@router.post("/api/auth/reset-password/{token}")
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


@router.get("/api/admin/password-resets")
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


@router.post("/api/auth/avatar")
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
