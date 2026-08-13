import asyncio
import html
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from jose import JWTError, jwt
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, text

from ..database import get_db
from ..models import User, PasswordReset, LoginHistory
from ..auth import (
    verify_password,
    get_password_hash,
    create_access_token_for_user,
    get_current_user,
    get_admin_user,
    revoke_token,
    COOKIE_NAME,
    SECRET_KEY,
    ALGORITHM,
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
    _send_notification_email,
    _fmt_dt,
    optimize_image_bytes,
    _totp_attempts_exceeded,
    _record_failed_totp,
    _reset_failed_totp,
    _issue_recovery_codes,
    _consume_recovery_code,
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
logger = logging.getLogger(__name__)


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
    token = create_access_token_for_user(user)
    _set_auth_cookie(response, token, user.role)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


async def _record_login_attempt(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    username_attempted: str,
    success: bool,
    failure_reason: Optional[str],
    ip_address: str,
    user_agent: str,
) -> None:
    """Best-effort audit row for POST /api/auth/login (LoginHistory, models.py) —
    written on every attempt, success or failure. Never allowed to break the actual
    login flow: a failure writing this row is logged and swallowed rather than
    propagated, same "best-effort, never breaks the caller" posture as
    send_push()/copy_backup_offsite() elsewhere in this codebase. user_id is None
    for a failed attempt against a username that never resolved to a real account —
    that's exactly the case worth auditing, so it can't require an FK match."""
    try:
        db.add(LoginHistory(
            user_id=user_id,
            username_attempted=(username_attempted or "")[:64],
            success=success,
            failure_reason=failure_reason,
            ip_address=(ip_address or "")[:64] or None,
            user_agent=(user_agent or "")[:256] or None,
        ))
        await db.commit()
    except Exception:
        logger.exception("Failed to record login-history row for username=%r", username_attempted)
        await db.rollback()


async def _maybe_notify_new_device(db: AsyncSession, user: User, ip_address: str, user_agent: str) -> None:
    """Best-effort "new device" email, built on top of the LoginHistory audit trail
    _record_login_attempt() above writes. Fires only when this successful login's IP
    has never appeared on a PRIOR successful login for this account — deliberately
    IP-only, not IP+user-agent: simpler, and a changed browser on an already-trusted
    network is a much weaker signal than a brand-new network. Must be called BEFORE
    this login's own success row is recorded, otherwise the row would always find
    itself and never send anything.

    Skips the account's very first-ever successful login (no prior success rows at
    all) — that's just "welcome", not "new device", and shouldn't alarm someone right
    after registering. Never raises: a failure here must never affect the login
    response, matching send_newsletter_digest()'s "never breaks the caller" posture
    in helpers.py."""
    if not ip_address or ip_address == "unknown" or not user.email:
        return
    try:
        seen_this_ip = (await db.execute(
            select(LoginHistory.id).where(
                LoginHistory.user_id == user.id,
                LoginHistory.success.is_(True),
                LoginHistory.ip_address == ip_address,
            ).limit(1)
        )).scalar_one_or_none()
        if seen_this_ip is not None:
            return
        had_prior_success = (await db.execute(
            select(LoginHistory.id).where(
                LoginHistory.user_id == user.id, LoginHistory.success.is_(True)
            ).limit(1)
        )).scalar_one_or_none()
        if had_prior_success is None:
            return
        safe_username = html.escape(user.username)
        safe_ip = html.escape(ip_address)
        safe_ua = html.escape((user_agent or "неизвестно")[:200])
        asyncio.create_task(_send_notification_email(
            user.email,
            "Вход в аккаунт с нового устройства",
            f"Выполнен вход в ваш аккаунт {user.username} с IP-адреса, которого раньше не было в истории входов:\n\n"
            f"IP: {ip_address}\nУстройство: {user_agent or 'неизвестно'}\n\n"
            f"Если это были не вы — срочно смените пароль и включите двухфакторную аутентификацию в личном кабинете.",
            f"<p>Выполнен вход в ваш аккаунт <b>{safe_username}</b> с IP-адреса, которого раньше не было в истории входов:</p>"
            f"<p>IP: <b>{safe_ip}</b><br>Устройство: {safe_ua}</p>"
            f"<p>Если это были не вы — срочно смените пароль и включите двухфакторную аутентификацию в личном кабинете.</p>",
        ))
    except Exception:
        logger.exception("Failed new-device email check for user_id=%s", user.id)


@router.post("/api/auth/login", response_model=TokenOut)
@limiter.limit("10/minute")
async def login(request: Request, body: UserLogin, response: Response, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        logger.warning("Failed login for username=%r from ip=%s (invalid credentials)", body.username, client_ip)
        await _record_login_attempt(
            db, user_id=user.id if user else None, username_attempted=body.username,
            success=False, failure_reason="invalid_credentials", ip_address=client_ip, user_agent=user_agent,
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        await _record_login_attempt(
            db, user_id=user.id, username_attempted=body.username,
            success=False, failure_reason="account_inactive", ip_address=client_ip, user_agent=user_agent,
        )
        raise HTTPException(status_code=403, detail="Ваш аккаунт был заблокирован.")
    if user.totp_enabled:
        import pyotp
        # Secondary, per-account guard on top of the endpoint-wide @limiter.limit
        # above: that 10/minute quota is shared across every failure mode (wrong
        # password included) from anyone, so it doesn't stop a focused attacker who
        # already has the password from grinding through 6-digit TOTP codes for one
        # specific account. Independent of and in addition to that global limit.
        if _totp_attempts_exceeded(user.id):
            logger.warning("Failed login for username=%r from ip=%s (TOTP attempts exceeded)", body.username, client_ip)
            await _record_login_attempt(
                db, user_id=user.id, username_attempted=body.username,
                success=False, failure_reason="totp_rate_limited", ip_address=client_ip, user_agent=user_agent,
            )
            raise HTTPException(status_code=401, detail="Слишком много неверных попыток 2FA, попробуйте позже")
        # Accept either a live TOTP code or a single-use recovery code in the same
        # `totp_code` field (see frontend/login.html's "use a recovery code instead"
        # toggle) — this is the account's escape hatch for a lost authenticator
        # device, so it must count against and reset the same brute-force limiter as
        # a regular TOTP attempt (same account, same risk).
        used_recovery = False
        totp_ok = bool(body.totp_code) and pyotp.TOTP(user.totp_secret).verify(body.totp_code, valid_window=1)
        if not totp_ok and body.totp_code:
            used_recovery = await _consume_recovery_code(db, user.id, body.totp_code)
            totp_ok = used_recovery
        if not totp_ok:
            _record_failed_totp(user.id)
            logger.warning("Failed login for username=%r from ip=%s (invalid TOTP code)", body.username, client_ip)
            await _record_login_attempt(
                db, user_id=user.id, username_attempted=body.username,
                success=False, failure_reason=("totp_required" if not body.totp_code else "invalid_totp"),
                ip_address=client_ip, user_agent=user_agent,
            )
            raise HTTPException(status_code=401, detail="Требуется код 2FA")
        if used_recovery:
            # Persist the used_at stamp from _consume_recovery_code() so the same
            # code can never be replayed.
            await db.commit()
            logger.info("Login for username=%r used a 2FA recovery code (ip=%s)", body.username, client_ip)
        _reset_failed_totp(user.id)
    token = create_access_token_for_user(user)
    _set_auth_cookie(response, token, user.role)
    # Must run before _record_login_attempt below — it needs to see prior successful
    # logins WITHOUT this one already counted, otherwise it would always find itself.
    await _maybe_notify_new_device(db, user, client_ip, user_agent)
    await _record_login_attempt(
        db, user_id=user.id, username_attempted=body.username,
        success=True, failure_reason=None, ip_address=client_ip, user_agent=user_agent,
    )
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


@router.get("/api/auth/session-expiry")
async def session_expiry(request: Request, current_user: User = Depends(get_current_user)):
    """Lightweight companion to GET /api/auth/me for admin.html's session-expiry
    warning (see CLAUDE.md's ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES note — admin/superadmin
    tokens now expire after 12h instead of the normal 7 days). Returns just the JWT's
    own `exp` claim so the frontend can warn before the session dies mid-edit, without
    ever persisting the raw token client-side — login.html deliberately never stores
    the access_token it gets back (see its comment on why), and this endpoint doesn't
    change that: it re-decodes the same httpOnly cookie/Bearer token
    get_current_user(...) above already validated, and hands back only the derived
    timestamp, never the token itself.

    get_current_user already proves the token is present, unrevoked and unexpired, so
    the decode below can't meaningfully fail — but it's wrapped anyway rather than
    trusting that invariant blindly across two functions."""
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token") from None
    exp = payload.get("exp")
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None
    return {"expires_at": expires_at}


@router.get("/api/auth/current-session")
async def current_session(request: Request, current_user: User = Depends(get_current_user)):
    """Small, honest substitute for true per-session listing on frontend/profile.html's
    security tab. This app is stateless JWT-in-a-cookie plus one revoke_before cutoff
    column (see POST /api/auth/logout-everywhere's docstring above) — there is no
    per-token session store, so there is no way to enumerate or individually revoke
    OTHER active sessions/devices. Real per-device tracking would need an actual
    sessions table (device fingerprint, issued-at, revoked flag) keyed by token, which
    is a materially bigger feature than this pass. What IS honest to show: THIS
    request's own token — the IP/device currently looking at the page, and when this
    specific token was issued/expires. Reuses the same re-decode approach as
    GET /api/auth/session-expiry just above (never persists the token, only derives
    values already proven valid by get_current_user's dependency)."""
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token") from None
    iat = payload.get("iat")
    exp = payload.get("exp")
    return {
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "issued_at": datetime.fromtimestamp(iat, tz=timezone.utc).isoformat() if iat else None,
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None,
        "last_active_at": _fmt_dt(current_user.last_active_at),
    }


@router.get("/api/auth/login-history")
async def get_login_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    """Paginated self-service view of this account's own POST /api/auth/login
    attempts (LoginHistory, models.py — written by _record_login_attempt() above),
    success and failure both. Strictly scoped to LoginHistory.user_id ==
    current_user.id: there is no way to see another user's history through this
    endpoint, admin or not — a separate admin-facing view would need its own
    dedicated endpoint, out of scope here."""
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    result = await db.execute(
        select(LoginHistory)
        .where(LoginHistory.user_id == current_user.id)
        .order_by(LoginHistory.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.scalars().all()
    total = (await db.execute(
        select(func.count(LoginHistory.id)).where(LoginHistory.user_id == current_user.id)
    )).scalar_one()
    return {
        "items": [
            {
                "id": r.id,
                "success": r.success,
                "failure_reason": r.failure_reason,
                "ip_address": r.ip_address,
                "user_agent": r.user_agent,
                "created_at": _fmt_dt(r.created_at),
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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
    token = create_access_token_for_user(user)
    _set_auth_cookie(response, token, user.role)
    return {"ok": True, "access_token": token}


@router.post("/api/auth/logout-everywhere")
@limiter.limit("5/minute")
async def logout_everywhere(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Self-service "log out of all my other devices" — the same revoke_before
    mechanism as POST /api/admin/users/{user_id}/revoke-sessions
    (backend/routers/users.py), just reachable by a normal user acting on
    themselves: no target-user-id parameter, so there's no way to touch anyone
    else's sessions through this endpoint. Real per-device session tracking (a list
    of "active sessions" with device/location) isn't feasible without building actual
    session storage — this app is stateless-JWT-in-a-cookie plus this one
    revoke_before cutoff column, nothing per-token — so that's deliberately not
    attempted here; this just makes the existing all-or-nothing mechanism
    self-service instead of admin-only.

    Same 1-second backdate + immediate token reissue as change_password just above,
    and for the same reason: create_access_token's `iat` is a whole-second JWT
    NumericDate, but `revoke_before` here has microsecond precision, so an
    un-backdated "now" cutoff could end up *later* than the `iat` of a token minted
    in the same wall-clock second — including this request's own still-in-flight
    token, and the freshly reissued one below — which would paradoxically log the
    caller themselves out. Reissuing (not just avoiding revocation) also mirrors
    login: the browser tab that clicked the button gets a fresh cookie, so it stays
    signed in while every OTHER token issued before this moment is rejected on its
    next request.
    """
    now_utc = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await db.execute(text("UPDATE users SET revoke_before = :ts WHERE id = :uid"), {"ts": now_utc, "uid": current_user.id})
    await db.commit()
    token = create_access_token_for_user(current_user)
    _set_auth_cookie(response, token, current_user.role)
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
@limiter.limit("5/minute")
async def totp_enable(
    request: Request,
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
    # Issue the recovery-code batch in the same request that turns 2FA on — a user
    # locked out later with no codes saved would otherwise have no way back in
    # short of an admin editing the DB by hand. Plaintext codes are returned here
    # ONCE; only their bcrypt hashes are kept (see TotpRecoveryCode in models.py).
    recovery_codes = await _issue_recovery_codes(db, current_user.id)
    await db.commit()
    _totp_pending.pop(current_user.id, None)
    return {"ok": True, "recovery_codes": recovery_codes}


@router.post("/api/auth/2fa/disable")
@limiter.limit("5/minute")
async def totp_disable(
    request: Request,
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


@router.post("/api/auth/2fa/recovery-codes/regenerate")
@limiter.limit("5/minute")
async def totp_recovery_codes_regenerate(
    request: Request,
    body: TotpCodeBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invalidates every unused recovery code and issues a fresh batch — for a user
    who still has their authenticator (e.g. they suspect an old code leaked, or just
    burned through several) and wants a clean set. Gated behind a live TOTP code,
    same friction level as /2fa/disable, since this is a security-sensitive action
    on an already-enabled account (not the "I lost my authenticator" case — that's
    what the recovery codes themselves are for)."""
    import pyotp
    if not current_user.totp_enabled:
        raise HTTPException(400, "2FA не включена")
    if not pyotp.TOTP(current_user.totp_secret).verify(body.code, valid_window=1):
        raise HTTPException(400, "Неверный код")
    recovery_codes = await _issue_recovery_codes(db, current_user.id)
    await db.commit()
    return {"ok": True, "recovery_codes": recovery_codes}


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
    content = optimize_image_bytes(content, ext)
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
