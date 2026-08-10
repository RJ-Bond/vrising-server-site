"""Coverage for POST /api/auth/logout-everywhere (backend/routers/auth.py) — the
self-service version of POST /api/admin/users/{user_id}/revoke-sessions
(backend/routers/users.py), reachable by a normal authenticated user acting only on
themselves (the endpoint takes no target-user-id, so there's nothing to even try to
point at someone else). Same revoke_before + reissue pattern already covered for
change-password in test_security_fixes.py — mirrored here."""
import asyncio

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(username=username, email=f"{username.lower()}@example.com", hashed_password=get_password_hash("password1"), role=role)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_logout_everywhere_requires_auth(client, db_session):
    r = await client.post("/api/auth/logout-everywhere")
    assert r.status_code == 401


async def test_logout_everywhere_revokes_old_token_but_caller_stays_logged_in(client, db_session):
    user = await _make_user(db_session, "LogoutEverywhereUser")
    # Same whole-second `iat` precision issue as test_security_fixes.py's
    # change-password test — sleep past the second boundary so this genuinely
    # predates the revoke-everywhere call, like a real earlier session would.
    old_token = create_access_token({"sub": str(user.id)})
    await asyncio.sleep(1.1)

    r = await client.post("/api/auth/logout-everywhere", headers=_bearer(old_token))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    new_token = body["access_token"]
    assert new_token and new_token != old_token

    # The token that made the call itself predates the bump, so it must now be
    # rejected on its next use — that's the whole "every other device" guarantee.
    r_old = await client.get("/api/auth/me", headers=_bearer(old_token))
    assert r_old.status_code == 401

    # But the freshly reissued token (what the browser tab that clicked the button
    # actually keeps using, via the re-set cookie) still works — the caller's own
    # current session must survive its own logout-everywhere call.
    r_new = await client.get("/api/auth/me", headers=_bearer(new_token))
    assert r_new.status_code == 200
    assert r_new.json()["username"] == "LogoutEverywhereUser"


async def test_concurrent_logout_everywhere_calls_dont_error_or_lock_out_the_caller(client, db_session):
    """Two browser tabs both clicking "log out everywhere" at nearly the same instant
    is a realistic double-click/double-tab scenario, not a contrived race. The endpoint's
    own docstring (backend/routers/auth.py logout_everywhere()) explains the 1-second
    revoke_before backdate exists specifically so a `revoke_before` write from one
    request can never land later than the `iat` of a token another concurrent request
    already minted in the same wall-clock second — this test exercises exactly that
    with two real concurrent requests (asyncio.gather, not sequential awaits) rather
    than just trusting the docstring's reasoning."""
    user = await _make_user(db_session, "LogoutEverywhereConcurrent")
    old_token = create_access_token({"sub": str(user.id)})
    await asyncio.sleep(1.1)

    r1, r2 = await asyncio.gather(
        client.post("/api/auth/logout-everywhere", headers=_bearer(old_token)),
        client.post("/api/auth/logout-everywhere", headers=_bearer(old_token)),
    )

    # Neither concurrent call should error (500) or otherwise fail — both are the same
    # account performing the same self-service action at the same time.
    assert r1.status_code == 200
    assert r2.status_code == 200
    token1 = r1.json()["access_token"]
    token2 = r2.json()["access_token"]
    assert token1 and token2

    # The pre-dating token used to make both calls must now be rejected everywhere...
    r_old = await client.get("/api/auth/me", headers=_bearer(old_token))
    assert r_old.status_code == 401

    # ...but BOTH freshly reissued tokens must still work — whichever tab's response
    # the browser actually keeps, that tab must not find itself logged out by the
    # other concurrent call's revoke_before write. This is the "don't break the
    # caller's own session" guarantee the concurrency test is here to prove.
    r_me1 = await client.get("/api/auth/me", headers=_bearer(token1))
    assert r_me1.status_code == 200
    assert r_me1.json()["username"] == "LogoutEverywhereConcurrent"

    r_me2 = await client.get("/api/auth/me", headers=_bearer(token2))
    assert r_me2.status_code == 200
    assert r_me2.json()["username"] == "LogoutEverywhereConcurrent"


async def test_logout_everywhere_only_ever_targets_the_caller(client, db_session):
    # No target-user-id parameter exists on this endpoint at all — the strongest
    # version of "can't target another user" is that there's nothing to pass. This
    # just confirms a second, unrelated user's own token is completely unaffected by
    # the first user's logout-everywhere call.
    caller = await _make_user(db_session, "LogoutEverywhereCaller")
    bystander = await _make_user(db_session, "LogoutEverywhereBystander")
    bystander_token = create_access_token({"sub": str(bystander.id)})
    await asyncio.sleep(1.1)

    r = await client.post("/api/auth/logout-everywhere", headers=_bearer(create_access_token({"sub": str(caller.id)})))
    assert r.status_code == 200

    r_bystander = await client.get("/api/auth/me", headers=_bearer(bystander_token))
    assert r_bystander.status_code == 200
    assert r_bystander.json()["username"] == "LogoutEverywhereBystander"
