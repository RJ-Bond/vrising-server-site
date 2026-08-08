"""Regression tests for backend/routers/messages.py: 1:1 direct messages (send/inbox/
conversation/delete), the admin broadcast fan-out, and the unread-count badge. See the
Message model in models.py. The core permission boundary this router relies on is
implicit query scoping (every read/write query filters by current_user.id as sender or
recipient) rather than an explicit ownership check — these tests confirm that scoping
actually holds, not just that the happy path works."""
import pytest
from sqlalchemy import select

from backend.auth import create_access_token, get_password_hash
from backend.models import Message, User

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


async def _make_message(db_session, sender, recipient, content="hi", read=False):
    msg = Message(sender_id=sender.id, recipient_id=recipient.id, content=content, read=read)
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg


# ─── POST /api/messages ───────────────────────────────────────────────────────

async def test_send_message_requires_auth(client, db_session):
    r = await client.post("/api/messages", json={"recipient_username": "nobody", "content": "hi"})
    assert r.status_code == 401


async def test_send_message_happy_path(client, db_session):
    sender = await _make_user(db_session, "Sender1")
    recipient = await _make_user(db_session, "Recipient1")

    r = await client.post(
        "/api/messages",
        json={"recipient_username": recipient.username, "content": "Привет!"},
        headers=_bearer(sender),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["sender"] == sender.username
    assert body["recipient"] == recipient.username
    assert body["content"] == "Привет!"


async def test_send_message_rejects_messaging_self(client, db_session):
    user = await _make_user(db_session, "SelfSender")
    r = await client.post(
        "/api/messages",
        json={"recipient_username": user.username, "content": "hi"},
        headers=_bearer(user),
    )
    assert r.status_code == 400


async def test_send_message_404_for_missing_recipient(client, db_session):
    sender = await _make_user(db_session, "Sender2")
    r = await client.post(
        "/api/messages",
        json={"recipient_username": "nonexistent_user", "content": "hi"},
        headers=_bearer(sender),
    )
    assert r.status_code == 404


async def test_send_message_rejects_empty_content(client, db_session):
    sender = await _make_user(db_session, "Sender3")
    recipient = await _make_user(db_session, "Recipient3")
    r = await client.post(
        "/api/messages",
        json={"recipient_username": recipient.username, "content": "   "},
        headers=_bearer(sender),
    )
    assert r.status_code == 422


# ─── GET /api/messages/unread-count ───────────────────────────────────────────

async def test_unread_count_requires_auth(client, db_session):
    r = await client.get("/api/messages/unread-count")
    assert r.status_code == 401


async def test_unread_count_only_counts_own_unread(client, db_session):
    a = await _make_user(db_session, "CountA")
    b = await _make_user(db_session, "CountB")
    c = await _make_user(db_session, "CountC")
    await _make_message(db_session, b, a, content="to a #1")
    await _make_message(db_session, b, a, content="to a #2")
    await _make_message(db_session, b, c, content="to c, not a")

    r = await client.get("/api/messages/unread-count", headers=_bearer(a))
    assert r.status_code == 200
    assert r.json()["count"] == 2


# ─── GET /api/messages/inbox ──────────────────────────────────────────────────

async def test_inbox_requires_auth(client, db_session):
    r = await client.get("/api/messages/inbox")
    assert r.status_code == 401


async def test_inbox_lists_conversations_with_unread_counts(client, db_session):
    a = await _make_user(db_session, "InboxA")
    b = await _make_user(db_session, "InboxB")
    await _make_message(db_session, b, a, content="hey")
    await _make_message(db_session, a, b, content="hey back")

    r = await client.get("/api/messages/inbox", headers=_bearer(a))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["partner"]["username"] == b.username
    assert body[0]["last_message"]["content"] == "hey back"


async def test_inbox_excludes_other_users_conversations(client, db_session):
    a = await _make_user(db_session, "InboxOtherA")
    b = await _make_user(db_session, "InboxOtherB")
    c = await _make_user(db_session, "InboxOtherC")
    await _make_message(db_session, b, c, content="not for a")

    r = await client.get("/api/messages/inbox", headers=_bearer(a))
    assert r.status_code == 200
    assert r.json() == []


# ─── GET /api/messages/with/{username} ────────────────────────────────────────

async def test_conversation_requires_auth(client, db_session):
    r = await client.get("/api/messages/with/someone")
    assert r.status_code == 401


async def test_conversation_404_for_missing_partner(client, db_session):
    user = await _make_user(db_session, "ConvUser1")
    r = await client.get("/api/messages/with/nonexistent", headers=_bearer(user))
    assert r.status_code == 404


async def test_conversation_marks_unread_as_read(client, db_session):
    a = await _make_user(db_session, "ConvA")
    b = await _make_user(db_session, "ConvB")
    msg = await _make_message(db_session, b, a, content="unread msg")

    r = await client.get(f"/api/messages/with/{b.username}", headers=_bearer(a))
    assert r.status_code == 200
    body = r.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "unread msg"

    await db_session.refresh(msg)
    assert msg.read is True


async def test_conversation_isolated_from_other_users_threads(client, db_session):
    """Ownership boundary: fetching /api/messages/with/{partner} as user C must not
    surface a conversation that only exists between A and B."""
    a = await _make_user(db_session, "IsoA")
    b = await _make_user(db_session, "IsoB")
    c = await _make_user(db_session, "IsoC")
    await _make_message(db_session, a, b, content="private between a and b")

    r = await client.get(f"/api/messages/with/{b.username}", headers=_bearer(c))
    assert r.status_code == 200
    assert r.json()["messages"] == []


# ─── DELETE /api/messages/{id} ────────────────────────────────────────────────

async def test_delete_message_requires_auth(client, db_session):
    r = await client.delete("/api/messages/1")
    assert r.status_code == 401


async def test_delete_message_happy_path_by_sender(client, db_session):
    sender = await _make_user(db_session, "DelSender1")
    recipient = await _make_user(db_session, "DelRecipient1")
    msg = await _make_message(db_session, sender, recipient, content="delete me")

    r = await client.delete(f"/api/messages/{msg.id}", headers=_bearer(sender))
    assert r.status_code == 204

    remaining = (await db_session.execute(select(Message).where(Message.id == msg.id))).scalar_one_or_none()
    assert remaining is None


async def test_delete_message_rejects_non_sender(client, db_session):
    """Ownership boundary: the recipient (or any other user) cannot delete a message
    they didn't send — the route only matches on sender_id == current_user.id, so a
    non-owner gets 404, not 403 (it doesn't reveal whether the message exists for them)."""
    sender = await _make_user(db_session, "DelSender2")
    recipient = await _make_user(db_session, "DelRecipient2")
    msg = await _make_message(db_session, sender, recipient, content="not yours")

    r = await client.delete(f"/api/messages/{msg.id}", headers=_bearer(recipient))
    assert r.status_code == 404

    still_there = (await db_session.execute(select(Message).where(Message.id == msg.id))).scalar_one_or_none()
    assert still_there is not None


async def test_delete_message_404_for_missing(client, db_session):
    user = await _make_user(db_session, "DelUser3")
    r = await client.delete("/api/messages/99999", headers=_bearer(user))
    assert r.status_code == 404


# ─── POST /api/admin/broadcast ────────────────────────────────────────────────

async def test_broadcast_requires_admin(client, db_session):
    user = await _make_user(db_session, "BroadcastUser1")
    r = await client.post("/api/admin/broadcast", json={"content": "hi all"}, headers=_bearer(user))
    assert r.status_code == 403


async def test_broadcast_rejects_moderator(client, db_session):
    """Broadcast is admin-tier, not moderator-tier — moderators must not slip through."""
    moderator = await _make_user(db_session, "BroadcastMod1", role="moderator")
    r = await client.post("/api/admin/broadcast", json={"content": "hi all"}, headers=_bearer(moderator))
    assert r.status_code == 403


async def test_broadcast_happy_path_sends_to_all_active_users(client, db_session):
    admin = await _make_user(db_session, "BroadcastAdmin1", role="admin")
    u1 = await _make_user(db_session, "BroadcastRecv1")
    u2 = await _make_user(db_session, "BroadcastRecv2")

    r = await client.post(
        "/api/admin/broadcast",
        json={"content": "Всем привет"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    assert r.json()["sent"] == 2

    for u in (u1, u2):
        inbox = (await db_session.execute(select(Message).where(Message.recipient_id == u.id))).scalars().all()
        assert len(inbox) == 1
        assert inbox[0].content == "Всем привет"

    admin_inbox = (await db_session.execute(select(Message).where(Message.recipient_id == admin.id))).scalars().all()
    assert admin_inbox == []


async def test_broadcast_filters_by_role(client, db_session):
    admin = await _make_user(db_session, "BroadcastAdmin2", role="admin")
    mod = await _make_user(db_session, "BroadcastRecvMod", role="moderator")
    plain = await _make_user(db_session, "BroadcastRecvPlain", role="user")

    r = await client.post(
        "/api/admin/broadcast",
        json={"content": "Only for mods", "role": "moderator"},
        headers=_bearer(admin),
    )
    assert r.status_code == 201
    assert r.json()["sent"] == 1

    mod_inbox = (await db_session.execute(select(Message).where(Message.recipient_id == mod.id))).scalars().all()
    assert len(mod_inbox) == 1

    plain_inbox = (await db_session.execute(select(Message).where(Message.recipient_id == plain.id))).scalars().all()
    assert plain_inbox == []
