"""Coverage for 2FA recovery codes — the escape hatch for a user who enables TOTP
(POST /api/auth/2fa/enable, backend/routers/auth.py) and later loses their
authenticator device. Before this, login() had no way back in for such a user short
of an admin editing the DB by hand (and there's no admin-side "disable 2FA for this
user" endpoint either — see backend/routers/admin_*.py, grep totp_enabled).

Exercises the real setup -> enable -> login/regenerate flow through the API (not a
helper-function shortcut) so these tests prove the actual response shape and
hashing-at-rest behavior. Authenticates setup/enable/regenerate calls via a Bearer
token (same `_bearer()` pattern as test_admin_role_tiers.py) rather than the login
endpoint's cookie, so these tests don't depend on cookie-jar persistence across
requests; the recovery-code-at-login tests below still go through the real
POST /api/auth/login."""

import re

import pyotp
import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.helpers import _failed_totp_attempts
from backend.models import TotpRecoveryCode, User

pytestmark = pytest.mark.asyncio

RECOVERY_CODE_RE = re.compile(r"^[0-9A-F]{4}-[0-9A-F]{4}$")
TOTP_REQUIRED_DETAIL = "Требуется код 2FA"


@pytest.fixture(autouse=True)
def _reset_totp_attempts():
    """_failed_totp_attempts is a process-global in-memory dict (see
    test_login_totp_bruteforce.py) — clear it before/after each test so runs don't
    leak into each other regardless of execution order."""
    _failed_totp_attempts.clear()
    yield
    _failed_totp_attempts.clear()


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _make_user(db_session, username, password="password1"):
    user = User(
        username=username, email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash(password), role="user",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _enable_totp(client, db_session, username, password="password1"):
    """Creates a fresh user and drives the real GET /2fa/setup -> POST /2fa/enable
    flow. Returns (enable_response_json, totp_secret, bearer_headers)."""
    user = await _make_user(db_session, username, password)
    headers = _bearer(user)

    setup_res = await client.get("/api/auth/2fa/setup", headers=headers)
    assert setup_res.status_code == 200
    secret = setup_res.json()["secret"]

    enable_res = await client.post("/api/auth/2fa/enable", json={"code": pyotp.TOTP(secret).now()}, headers=headers)
    assert enable_res.status_code == 200
    return enable_res.json(), secret, headers


async def test_enable_returns_ten_distinct_recovery_codes(client, db_session):
    data, _secret, _headers = await _enable_totp(client, db_session, "RecoveryEnable")
    codes = data["recovery_codes"]
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for c in codes:
        assert RECOVERY_CODE_RE.match(c), c


async def test_recovery_codes_are_hashed_at_rest_not_plaintext(client, db_session):
    data, _secret, _headers = await _enable_totp(client, db_session, "RecoveryHash")
    codes = data["recovery_codes"]

    user = (await db_session.execute(select(User).where(User.username == "RecoveryHash"))).scalar_one()
    rows = (await db_session.execute(
        select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id)
    )).scalars().all()

    assert len(rows) == 10
    for row in rows:
        assert row.used_at is None
        # bcrypt hash, not the plaintext code (or any recognizable fragment of it)
        assert row.code_hash.startswith(("$2a$", "$2b$"))
        assert row.code_hash not in codes
        assert all(code.replace("-", "").lower() not in row.code_hash for code in codes)


async def test_valid_unused_recovery_code_logs_in_and_gets_consumed(client, db_session):
    data, _secret, _headers = await _enable_totp(client, db_session, "RecoveryLogin")
    code = data["recovery_codes"][0]

    res = await client.post("/api/auth/login", json={
        "username": "RecoveryLogin", "password": "password1", "totp_code": code,
    })
    assert res.status_code == 200
    assert res.json()["user"]["username"] == "RecoveryLogin"

    user = (await db_session.execute(select(User).where(User.username == "RecoveryLogin"))).scalar_one()
    rows = (await db_session.execute(
        select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id)
    )).scalars().all()
    used = [r for r in rows if r.used_at is not None]
    assert len(used) == 1


async def test_recovery_code_accepted_without_dash_and_case_insensitive(client, db_session):
    data, _secret, _headers = await _enable_totp(client, db_session, "RecoveryFormat")
    code = data["recovery_codes"][1]
    loose = code.replace("-", "").lower()

    res = await client.post("/api/auth/login", json={
        "username": "RecoveryFormat", "password": "password1", "totp_code": loose,
    })
    assert res.status_code == 200


async def test_used_recovery_code_is_rejected_on_reuse(client, db_session):
    data, _secret, _headers = await _enable_totp(client, db_session, "RecoveryReuse")
    code = data["recovery_codes"][0]

    first = await client.post("/api/auth/login", json={
        "username": "RecoveryReuse", "password": "password1", "totp_code": code,
    })
    assert first.status_code == 200

    second = await client.post("/api/auth/login", json={
        "username": "RecoveryReuse", "password": "password1", "totp_code": code,
    })
    assert second.status_code == 401
    assert second.json()["detail"] == TOTP_REQUIRED_DETAIL


async def test_regenerate_requires_valid_totp_code(client, db_session):
    _data, _secret, headers = await _enable_totp(client, db_session, "RecoveryRegenBad")
    res = await client.post("/api/auth/2fa/recovery-codes/regenerate", json={"code": "000000"}, headers=headers)
    assert res.status_code == 400


async def test_regenerate_invalidates_old_codes_and_issues_a_fresh_batch(client, db_session):
    data, secret, headers = await _enable_totp(client, db_session, "RecoveryRegen")
    old_codes = data["recovery_codes"]

    regen_res = await client.post("/api/auth/2fa/recovery-codes/regenerate", json={
        "code": pyotp.TOTP(secret).now(),
    }, headers=headers)
    assert regen_res.status_code == 200
    new_codes = regen_res.json()["recovery_codes"]
    assert len(new_codes) == 10
    assert set(new_codes).isdisjoint(old_codes)

    # An old (pre-regenerate) code must no longer work for login.
    res_old = await client.post("/api/auth/login", json={
        "username": "RecoveryRegen", "password": "password1", "totp_code": old_codes[0],
    })
    assert res_old.status_code == 401
    assert res_old.json()["detail"] == TOTP_REQUIRED_DETAIL

    # A freshly issued code still works.
    res_new = await client.post("/api/auth/login", json={
        "username": "RecoveryRegen", "password": "password1", "totp_code": new_codes[0],
    })
    assert res_new.status_code == 200


async def test_wrong_recovery_code_counts_against_totp_bruteforce_limiter(client, db_session):
    """A failed recovery-code attempt is the same account-risk as a failed live TOTP
    attempt, so it must count against the same per-account limiter (backend/helpers.py
    _totp_attempts_exceeded/_record_failed_totp) — see test_login_totp_bruteforce.py
    for the equivalent live-TOTP-code coverage this mirrors."""
    data, _secret, _headers = await _enable_totp(client, db_session, "RecoveryBrute")
    valid_code = data["recovery_codes"][0]

    statuses = []
    for _ in range(5):
        r = await client.post("/api/auth/login", json={
            "username": "RecoveryBrute", "password": "password1", "totp_code": "ZZZZ-ZZZZ",
        })
        statuses.append(r.status_code)
    assert statuses == [401] * 5

    # 6th attempt, this time with an actually-valid, still-unused recovery code —
    # must still be rejected, proving the block is on the account/attempt-count.
    r = await client.post("/api/auth/login", json={
        "username": "RecoveryBrute", "password": "password1", "totp_code": valid_code,
    })
    assert r.status_code == 401


async def test_successful_recovery_code_login_resets_attempt_counter(client, db_session):
    data, _secret, _headers = await _enable_totp(client, db_session, "RecoveryReset")
    valid_code = data["recovery_codes"][0]

    for _ in range(3):
        r = await client.post("/api/auth/login", json={
            "username": "RecoveryReset", "password": "password1", "totp_code": "000000",
        })
        assert r.status_code == 401

    r = await client.post("/api/auth/login", json={
        "username": "RecoveryReset", "password": "password1", "totp_code": valid_code,
    })
    assert r.status_code == 200

    user = (await db_session.execute(select(User).where(User.username == "RecoveryReset"))).scalar_one()
    assert user.id not in _failed_totp_attempts
