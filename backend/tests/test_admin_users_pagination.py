"""Regression tests for GET /api/admin/users's page/per_page bounding.

No prior test file covered this endpoint's listing behavior directly (only
test_admin_role_tiers.py's tier matrix, which just checks status codes) — this endpoint
used to return every single User row with no limit at all, an unbounded-growth risk as
the users table grows. It keeps returning a bare JSON array (response_model=list[UserOut])
rather than switching to the page/per_page/total/items wrapper used elsewhere in this
router, because admin.html's loadUsers()/renderUsersTable() fetch it expecting exactly
that shape and do their own client-side sort/filter/paginate over the result — total row
count is exposed via the X-Total-Count response header instead, additive and non-breaking.
"""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_admin(db_session, username="UsersPageAdmin"):
    admin = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("adminpass1"),
        role="admin",
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    return admin


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def test_admin_users_response_is_still_a_bare_array(client, db_session):
    admin = await _make_admin(db_session)
    r = await client.get("/api/admin/users", headers=_bearer(admin))
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_admin_users_pagination_pages_dont_overlap_and_header_total_is_correct(client, db_session):
    admin = await _make_admin(db_session)
    for i in range(5):
        db_session.add(User(
            username=f"PageUser{i}",
            email=f"pageuser{i}@example.com",
            hashed_password=get_password_hash("password1"),
            role="user",
        ))
    await db_session.commit()

    page1 = await client.get(
        "/api/admin/users", params={"page": 1, "per_page": 2}, headers=_bearer(admin)
    )
    page2 = await client.get(
        "/api/admin/users", params={"page": 2, "per_page": 2}, headers=_bearer(admin)
    )
    assert page1.status_code == 200 and page2.status_code == 200
    usernames_1 = [u["username"] for u in page1.json()]
    usernames_2 = [u["username"] for u in page2.json()]
    assert len(usernames_1) == 2
    assert len(usernames_2) == 2
    assert set(usernames_1).isdisjoint(usernames_2)
    # 5 seeded users + the admin making the request = 6 total rows.
    assert page1.headers["x-total-count"] == "6"
    assert page2.headers["x-total-count"] == "6"
