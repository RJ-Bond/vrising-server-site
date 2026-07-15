"""Regression tests for server-side HTML-tag stripping on comment/DM content —
defense-in-depth so a stored comment/DM can never contain live HTML tags, even if
some future rendering path forgets to escape/sanitize on the way out."""
from datetime import datetime, timezone

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import News, User
from backend.schemas import strip_html_tags

pytestmark = pytest.mark.asyncio


def test_strip_html_tags_removes_tags_keeps_text():
    assert strip_html_tags("hello <b>world</b>") == "hello world"
    assert strip_html_tags('<a href="evil.example">click me</a>') == "click me"
    assert strip_html_tags("<script>alert(1)</script>") == "alert(1)"
    assert strip_html_tags("plain text, no tags") == "plain text, no tags"


async def _make_user(db_session, username):
    user = User(username=username, email=f"{username}@example.com", hashed_password=get_password_hash("x"), role="user")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_comment_creation_strips_html_tags(client, db_session):
    author = await _make_user(db_session, "newsauthor")
    news = News(
        title="Test News", slug="test-news", summary="s", content="c",
        author_id=author.id, published=True,
    )
    db_session.add(news)
    await db_session.commit()

    commenter = await _make_user(db_session, "commenter")
    token = create_access_token({"sub": str(commenter.id)})

    r = await client.post(
        "/api/news/test-news/comments",
        json={"content": 'Nice post <script>alert(1)</script> <a href="evil">click</a>'},
        headers=_bearer(token),
    )
    assert r.status_code == 201
    stored = r.json()["content"]
    assert "<script>" not in stored
    assert "<a " not in stored
    assert "alert(1)" in stored  # text content survives, just not as a live tag
    assert "click" in stored


async def test_dm_creation_strips_html_tags(client, db_session):
    sender = await _make_user(db_session, "sender1")
    recipient = await _make_user(db_session, "recipient1")
    token = create_access_token({"sub": str(sender.id)})

    r = await client.post(
        "/api/messages",
        json={"recipient_username": "recipient1", "content": "hey <img src=x onerror=alert(1)>"},
        headers=_bearer(token),
    )
    assert r.status_code == 201
    stored = r.json()["content"]
    # The whole start-tag (including its onerror="..." attribute) is consumed as
    # markup, not text — HTMLParser only surfaces text *between* tags — so nothing
    # from inside the tag itself should survive, only the surrounding plain text.
    assert stored.strip() == "hey"
    assert "<img" not in stored and "onerror" not in stored
