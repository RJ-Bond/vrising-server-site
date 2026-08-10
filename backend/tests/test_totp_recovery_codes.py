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


async def test_totp_code_field_is_ignored_for_an_account_without_2fa_enabled(client, db_session):
    """login()'s entire TOTP/recovery-code branch (backend/routers/auth.py) lives
    inside `if user.totp_enabled:` — for an account that never enabled 2FA, that
    block never runs at all, so a submitted `totp_code` (live or recovery-shaped)
    isn't validated, rejected, or even looked at; it's simply ignored and a correct
    username/password alone is enough to log in. Confirmed by reading login() before
    writing this — the alternative (a stray totp_code triggering a 400/401 because
    the account isn't enrolled) is NOT what's implemented, so this test asserts the
    real behavior rather than the more "obviously correct"-sounding one."""
    user = User(
        username="NoTwoFactorUser", email="notwofactoruser@example.com",
        hashed_password=get_password_hash("password1"), role="user",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    assert user.totp_enabled is False

    # No TotpRecoveryCode rows exist for this user at all — this is not a real
    # recovery code, just an arbitrarily formatted string, to prove the field is
    # never even consulted (there's nothing valid it COULD match against).
    res = await client.post("/api/auth/login", json={
        "username": "NoTwoFactorUser", "password": "password1", "totp_code": "AAAA-BBBB",
    })
    assert res.status_code == 200
    assert res.json()["user"]["username"] == "NoTwoFactorUser"

    # Omitting totp_code entirely must behave identically — confirms the field is
    # simply irrelevant for this account, not "accepted as a no-op password bypass".
    res2 = await client.post("/api/auth/login", json={
        "username": "NoTwoFactorUser", "password": "password1",
    })
    assert res2.status_code == 200


async def test_regenerate_invalidates_the_entire_old_batch_not_just_already_used_codes(client, db_session):
    """_issue_recovery_codes() (backend/helpers.py) invalidates old codes with a bulk
    UPDATE ... WHERE used_at IS NULL — i.e. it targets every still-unused code in one
    statement rather than iterating some other list, so there's no code path by which
    an old code could be skipped just because a sibling in the same batch happened to
    already be used. This test creates exactly that mixed situation (one old code
    already consumed, the rest untouched) before regenerating, so a hypothetical bug
    that only invalidated "used" or only "the first N" old codes would be caught —
    unlike test_regenerate_invalidates_old_codes_and_issues_a_fresh_batch, which only
    ever checks an old code that was never used, this specifically confirms the
    still-unused survivors of a partially-used batch are invalidated too."""
    data, secret, headers = await _enable_totp(client, db_session, "RecoveryRegenMixed")
    old_codes = data["recovery_codes"]

    # Consume one old code via a real login first, leaving 9 old codes unused.
    used_login = await client.post("/api/auth/login", json={
        "username": "RecoveryRegenMixed", "password": "password1", "totp_code": old_codes[0],
    })
    assert used_login.status_code == 200

    # Snapshot the original batch's row ids before regenerating — the "sanity check
    # the new batch still works" login below consumes one of the NEW rows too, so the
    # final assertion needs to check specifically the original 10 rows rather than
    # "all used_at IS NOT NULL rows across both batches", which would overcount by 1.
    user_before = (await db_session.execute(select(User).where(User.username == "RecoveryRegenMixed"))).scalar_one()
    original_ids = {
        r.id for r in (await db_session.execute(
            select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user_before.id)
        )).scalars().all()
    }

    regen_res = await client.post("/api/auth/2fa/recovery-codes/regenerate", json={
        "code": pyotp.TOTP(secret).now(),
    }, headers=headers)
    assert regen_res.status_code == 200
    new_codes = regen_res.json()["recovery_codes"]

    # Spot-check two of the old codes that were still unused at regenerate-time —
    # not old_codes[0], which was already used before regen and would trivially fail
    # anyway. (Deliberately just a couple of the 9 via login, not all of them: POST
    # /api/auth/login also carries its own endpoint-wide @limiter.limit("10/minute")
    # per IP — shared across every login call this test makes, unrelated to and much
    # tighter than the per-account TOTP brute-force limiter — so looping over all 9
    # here would 429 before reaching them all. The DB-level assertion below checks
    # the entire batch directly and doesn't spend any of that quota.)
    for stale_code in (old_codes[1], old_codes[5]):
        res = await client.post("/api/auth/login", json={
            "username": "RecoveryRegenMixed", "password": "password1", "totp_code": stale_code,
        })
        assert res.status_code == 401, f"stale unused old code {stale_code!r} should have been invalidated by regenerate"
        assert res.json()["detail"] == TOTP_REQUIRED_DETAIL
    _failed_totp_attempts.clear()

    # Sanity check the new batch still works.
    res_new = await client.post("/api/auth/login", json={
        "username": "RecoveryRegenMixed", "password": "password1", "totp_code": new_codes[0],
    })
    assert res_new.status_code == 200

    # And at the DB level: all 10 rows from the original batch must be marked used —
    # not just the one actually consumed via login, and not just the two spot-checked
    # above. This is the authoritative check that the entire old batch was invalidated.
    # Scoped to original_ids (not "all rows"): the new batch also has one used row now
    # (new_codes[0], consumed by the sanity-check login above), which is correct and
    # expected — it just isn't what this assertion is about.
    rows = (await db_session.execute(
        select(TotpRecoveryCode).where(TotpRecoveryCode.id.in_(original_ids))
    )).scalars().all()
    assert len(rows) == 10
    used_rows = [r for r in rows if r.used_at is not None]
    assert len(used_rows) == 10  # the entire original batch, regardless of which were actually consumed
