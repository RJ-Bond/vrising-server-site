"""Regression tests for backend/routers/polls.py: admin-only poll creation on a
news post (one poll per post), public poll retrieval with vote tallies and the
viewer's own selections, the vote endpoint (single vs multiple choice, no
double-voting, expiry cutoff), and admin-only deletion. See the Poll/PollOption/
PollVote models in models.py and PollCreate/PollOptionCreate in schemas.py."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import News, Poll, PollOption, PollVote, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _make_news(db_session, author, slug="test-post", title="Test Post"):
    news = News(
        title=title,
        slug=slug,
        summary="summary",
        content="content",
        author_id=author.id,
    )
    db_session.add(news)
    await db_session.commit()
    await db_session.refresh(news)
    return news


# ─── POST /api/news/{slug}/poll (admin create) ───────────────────────────────

async def test_create_poll_requires_admin(client, db_session):
    author = await _make_user(db_session, "PollAuthor1", role="admin")
    user = await _make_user(db_session, "PollUser1", role="user")
    news = await _make_news(db_session, author, slug="post-1")

    r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Best build?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(user),
    )
    assert r.status_code == 403


async def test_create_poll_happy_path(client, db_session):
    admin = await _make_user(db_session, "PollAdmin1", role="admin")
    news = await _make_news(db_session, admin, slug="post-2")

    r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Best build?", "multiple": False, "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ok"] is True

    poll = (await db_session.execute(select(Poll).where(Poll.id == body["poll_id"]))).scalar_one()
    assert poll.question == "Best build?"
    opts = (await db_session.execute(select(PollOption).where(PollOption.poll_id == poll.id))).scalars().all()
    assert len(opts) == 2


async def test_create_poll_404_for_missing_news(client, db_session):
    admin = await _make_user(db_session, "PollAdmin2", role="admin")
    r = await client.post(
        "/api/news/does-not-exist/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    assert r.status_code == 404


async def test_create_poll_rejects_duplicate_for_same_news(client, db_session):
    admin = await _make_user(db_session, "PollAdmin3", role="admin")
    news = await _make_news(db_session, admin, slug="post-3")

    r1 = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q1?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    assert r1.status_code == 201

    r2 = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q2?", "options": [{"text": "C"}, {"text": "D"}]},
        headers=_bearer(admin),
    )
    assert r2.status_code == 400


# ─── GET /api/news/{slug}/poll ───────────────────────────────────────────────

async def test_get_poll_returns_none_when_no_poll(client, db_session):
    admin = await _make_user(db_session, "PollAdmin4", role="admin")
    news = await _make_news(db_session, admin, slug="post-4")

    r = await client.get(f"/api/news/{news.slug}/poll")
    assert r.status_code == 200
    assert r.json() is None


async def test_get_poll_404_for_missing_news(client, db_session):
    r = await client.get("/api/news/nope/poll")
    assert r.status_code == 404


async def test_get_poll_shows_vote_tallies(client, db_session):
    """Vote counts are tallied per option regardless of who's viewing."""
    admin = await _make_user(db_session, "PollAdmin5", role="admin")
    voter = await _make_user(db_session, "PollVoter1", role="user")
    news = await _make_news(db_session, admin, slug="post-5")
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Pick one", "options": [{"text": "Opt A"}, {"text": "Opt B"}]},
        headers=_bearer(admin),
    )
    poll_id = create_r.json()["poll_id"]
    opt_a_id = (await db_session.execute(
        select(PollOption).where(PollOption.poll_id == poll_id, PollOption.text == "Opt A")
    )).scalar_one().id

    vote_r = await client.post(
        f"/api/news/{news.slug}/poll/vote",
        json={"option_ids": [opt_a_id]},
        headers=_bearer(voter),
    )
    assert vote_r.status_code == 200

    r = await client.get(f"/api/news/{news.slug}/poll", headers=_bearer(voter))
    assert r.status_code == 200
    body = r.json()
    assert body["total_votes"] == 1
    opt_a = next(o for o in body["options"] if o["id"] == opt_a_id)
    assert opt_a["votes"] == 1


async def test_get_poll_user_voted_reflects_bearer_authenticated_voter(client, db_session):
    """get_poll used to resolve the viewer by calling get_current_user(request=request,
    db=db) directly instead of via FastAPI's Depends() injection, so its `credentials`
    parameter never got the resolved HTTPAuthorizationCredentials — it kept the
    literal Depends(bearer_scheme) default object, `.credentials` on that raised, and
    the surrounding try/except swallowed it, so user_voted was always [] regardless of
    auth method. Fixed by taking current_user: Optional[User] = Depends(get_optional_user)
    as a real parameter instead."""
    admin = await _make_user(db_session, "PollAdmin5b", role="admin")
    voter = await _make_user(db_session, "PollVoter1b", role="user")
    news = await _make_news(db_session, admin, slug="post-5b")
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Pick one", "options": [{"text": "Opt A"}, {"text": "Opt B"}]},
        headers=_bearer(admin),
    )
    poll_id = create_r.json()["poll_id"]
    opt_a_id = (await db_session.execute(
        select(PollOption).where(PollOption.poll_id == poll_id, PollOption.text == "Opt A")
    )).scalar_one().id

    vote_r = await client.post(
        f"/api/news/{news.slug}/poll/vote",
        json={"option_ids": [opt_a_id]},
        headers=_bearer(voter),
    )
    assert vote_r.status_code == 200

    r = await client.get(f"/api/news/{news.slug}/poll", headers=_bearer(voter))
    assert r.status_code == 200
    assert r.json()["user_voted"] == [opt_a_id]


async def test_get_poll_anonymous_has_empty_user_voted(client, db_session):
    admin = await _make_user(db_session, "PollAdmin6", role="admin")
    news = await _make_news(db_session, admin, slug="post-6")
    await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )

    r = await client.get(f"/api/news/{news.slug}/poll")
    assert r.status_code == 200
    assert r.json()["user_voted"] == []


# ─── POST /api/news/{slug}/poll/vote ─────────────────────────────────────────

async def test_vote_requires_login(client, db_session):
    admin = await _make_user(db_session, "PollAdmin7", role="admin")
    news = await _make_news(db_session, admin, slug="post-7")
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    opt_id = (await db_session.execute(
        select(PollOption).where(PollOption.poll_id == create_r.json()["poll_id"])
    )).scalars().first().id

    r = await client.post(f"/api/news/{news.slug}/poll/vote", json={"option_ids": [opt_id]})
    assert r.status_code == 401


async def test_vote_requires_option_ids(client, db_session):
    admin = await _make_user(db_session, "PollAdmin8", role="admin")
    voter = await _make_user(db_session, "PollVoter2", role="user")
    news = await _make_news(db_session, admin, slug="post-8")
    await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )

    r = await client.post(f"/api/news/{news.slug}/poll/vote", json={"option_ids": []}, headers=_bearer(voter))
    assert r.status_code == 400


async def test_vote_404_when_no_poll_exists(client, db_session):
    admin = await _make_user(db_session, "PollAdmin9", role="admin")
    voter = await _make_user(db_session, "PollVoter3", role="user")
    news = await _make_news(db_session, admin, slug="post-9")

    r = await client.post(f"/api/news/{news.slug}/poll/vote", json={"option_ids": [1]}, headers=_bearer(voter))
    assert r.status_code == 404


async def test_vote_invalid_option_id_rejected(client, db_session):
    admin = await _make_user(db_session, "PollAdmin10", role="admin")
    voter = await _make_user(db_session, "PollVoter4", role="user")
    news = await _make_news(db_session, admin, slug="post-10")
    await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )

    r = await client.post(f"/api/news/{news.slug}/poll/vote", json={"option_ids": [999999]}, headers=_bearer(voter))
    assert r.status_code == 400


async def test_double_vote_rejected(client, db_session):
    admin = await _make_user(db_session, "PollAdmin11", role="admin")
    voter = await _make_user(db_session, "PollVoter5", role="user")
    news = await _make_news(db_session, admin, slug="post-11")
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    opts = (await db_session.execute(
        select(PollOption).where(PollOption.poll_id == create_r.json()["poll_id"])
    )).scalars().all()

    r1 = await client.post(f"/api/news/{news.slug}/poll/vote", json={"option_ids": [opts[0].id]}, headers=_bearer(voter))
    assert r1.status_code == 200

    r2 = await client.post(f"/api/news/{news.slug}/poll/vote", json={"option_ids": [opts[1].id]}, headers=_bearer(voter))
    assert r2.status_code == 400


async def test_single_choice_poll_only_counts_first_option(client, db_session):
    admin = await _make_user(db_session, "PollAdmin12", role="admin")
    voter = await _make_user(db_session, "PollVoter6", role="user")
    news = await _make_news(db_session, admin, slug="post-12")
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "multiple": False, "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    opts = (await db_session.execute(
        select(PollOption).where(PollOption.poll_id == create_r.json()["poll_id"])
    )).scalars().all()

    r = await client.post(
        f"/api/news/{news.slug}/poll/vote",
        json={"option_ids": [opts[0].id, opts[1].id]},
        headers=_bearer(voter),
    )
    assert r.status_code == 200

    votes = (await db_session.execute(select(PollVote).where(PollVote.user_id == voter.id))).scalars().all()
    assert len(votes) == 1
    assert votes[0].option_id == opts[0].id


async def test_multiple_choice_poll_with_more_than_one_selection_stores_both_votes(client, db_session):
    """PollVote's unique constraint used to be just ("poll_id", "user_id") — one row
    per user per poll, full stop — but vote_poll's `multiple=True` path inserts one
    PollVote row per selected option_id, so selecting more than one option always hit
    an IntegrityError. Fixed by widening the constraint to ("poll_id", "option_id",
    "user_id") — see PollVote's own docstring and main.py's one-time table-rebuild
    migration for already-deployed DBs."""
    admin = await _make_user(db_session, "PollAdmin13", role="admin")
    voter = await _make_user(db_session, "PollVoter7", role="user")
    news = await _make_news(db_session, admin, slug="post-13")
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "multiple": True, "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    opts = (await db_session.execute(
        select(PollOption).where(PollOption.poll_id == create_r.json()["poll_id"])
    )).scalars().all()

    r = await client.post(
        f"/api/news/{news.slug}/poll/vote",
        json={"option_ids": [opts[0].id, opts[1].id]},
        headers=_bearer(voter),
    )
    assert r.status_code == 200

    poll_r = await client.get(f"/api/news/{news.slug}/poll", headers=_bearer(voter))
    body = poll_r.json()
    assert sorted(body["user_voted"]) == sorted(o.id for o in opts)
    assert body["total_votes"] == 2


async def test_vote_after_poll_ended_rejected(client, db_session):
    admin = await _make_user(db_session, "PollAdmin14", role="admin")
    voter = await _make_user(db_session, "PollVoter8", role="user")
    news = await _make_news(db_session, admin, slug="post-14")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "ends_at": past, "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    opt_id = (await db_session.execute(
        select(PollOption).where(PollOption.poll_id == create_r.json()["poll_id"])
    )).scalars().first().id

    r = await client.post(f"/api/news/{news.slug}/poll/vote", json={"option_ids": [opt_id]}, headers=_bearer(voter))
    assert r.status_code == 400


# ─── DELETE /api/news/{slug}/poll ────────────────────────────────────────────

async def test_delete_poll_requires_admin(client, db_session):
    admin = await _make_user(db_session, "PollAdmin15", role="admin")
    user = await _make_user(db_session, "PollUser2", role="user")
    news = await _make_news(db_session, admin, slug="post-15")
    await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )

    r = await client.delete(f"/api/news/{news.slug}/poll", headers=_bearer(user))
    assert r.status_code == 403


async def test_delete_poll_happy_path(client, db_session):
    admin = await _make_user(db_session, "PollAdmin16", role="admin")
    news = await _make_news(db_session, admin, slug="post-16")
    create_r = await client.post(
        f"/api/news/{news.slug}/poll",
        json={"question": "Q?", "options": [{"text": "A"}, {"text": "B"}]},
        headers=_bearer(admin),
    )
    poll_id = create_r.json()["poll_id"]

    r = await client.delete(f"/api/news/{news.slug}/poll", headers=_bearer(admin))
    assert r.status_code == 204

    remaining = await db_session.get(Poll, poll_id)
    assert remaining is None


async def test_delete_poll_404_for_missing_news(client, db_session):
    admin = await _make_user(db_session, "PollAdmin17", role="admin")
    r = await client.delete("/api/news/nope/poll", headers=_bearer(admin))
    assert r.status_code == 404


async def test_delete_poll_noop_when_news_has_no_poll(client, db_session):
    admin = await _make_user(db_session, "PollAdmin18", role="admin")
    news = await _make_news(db_session, admin, slug="post-17")
    r = await client.delete(f"/api/news/{news.slug}/poll", headers=_bearer(admin))
    assert r.status_code == 204
