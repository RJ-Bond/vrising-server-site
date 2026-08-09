"""Regression coverage for GET /api/messages/stream — the per-recipient SSE channel
that replaced index.js's 5s poll of the open DM conversation (helpers.dm_broadcast()/
_dm_sse_clients, called from POST /api/messages and POST /api/admin/broadcast in
backend/routers/messages.py).

Same direct-call approach as test_sse_endpoints.py/test_activity_broadcast_triggers.py:
this is an infinite-generator StreamingResponse, which httpx's ASGITransport can't
drive through client.stream() (it blocks entering the context manager until the
generator yields, which never resolves the way a real socket connection would — see
test_sse_endpoints.py's module docstring for the full explanation). Calling the
endpoint function directly and reading response.body_iterator sidesteps the
transport entirely while still exercising the real generator code.

Unlike the activity feed's flat _activity_sse_clients set, _dm_sse_clients is keyed by
recipient user_id, so these tests additionally cover that a broadcast to one user's
queue never leaks to another user's — the whole reason this needed a dict instead of
reusing activity_broadcast()'s pattern.

No module-level `pytestmark = pytest.mark.asyncio` here (unlike test_search.py) —
mirrors test_sse_endpoints.py instead, since this file mixes plain sync tests
(route registration) with async ones and pytest-asyncio's strict mode warns on a
sync function picking up an async-only mark."""
import asyncio
import json

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.helpers import _dm_sse_clients, dm_broadcast
from backend.main import app
from backend.models import User
from backend.routers.messages import messages_stream


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


def test_messages_stream_route_registered():
    paths = app.openapi()["paths"]
    assert "/api/messages/stream" in paths
    assert "get" in paths["/api/messages/stream"]


@pytest.fixture(autouse=True)
def _clear_dm_sse_clients():
    """_dm_sse_clients is module-level global state — clear it before and after every
    test in this file so a queue left behind by one test (e.g. a direct-call response
    whose generator was never driven to its `finally`) can't leak into another."""
    _dm_sse_clients.clear()
    yield
    _dm_sse_clients.clear()


@pytest.mark.asyncio
async def test_messages_stream_requires_auth(client, db_session):
    r = await client.get("/api/messages/stream")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_messages_stream_returns_correctly_headed_streaming_response(db_session):
    recipient = await _make_user(db_session, "DmSseRecipient")
    response = await messages_stream(current_user=recipient)
    assert response.media_type == "text/event-stream"
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("x-accel-buffering") == "no"


@pytest.mark.asyncio
async def test_messages_stream_registers_queue_under_recipient_id(db_session):
    recipient = await _make_user(db_session, "DmSseRegister")
    assert recipient.id not in _dm_sse_clients
    response = await messages_stream(current_user=recipient)
    # Entering the generator far enough to register the queue requires pulling at
    # least once — the registration itself happens synchronously in the endpoint
    # function body though, before generate() is even defined, so it's already
    # visible without consuming from body_iterator.
    assert recipient.id in _dm_sse_clients
    assert len(_dm_sse_clients[recipient.id]) == 1
    del response


@pytest.mark.asyncio
async def test_dm_broadcast_delivers_to_connected_recipient(db_session):
    recipient = await _make_user(db_session, "DmSseDeliver")
    response = await messages_stream(current_user=recipient)
    try:
        dm_broadcast(recipient.id, {"from_username": "Sender1", "preview": "Привет"})
        chunk = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=5)
        assert "data: " in chunk
        payload = json.loads(chunk[len("data: "):].strip())
        assert payload["from_username"] == "Sender1"
        assert payload["preview"] == "Привет"
    finally:
        _dm_sse_clients.pop(recipient.id, None)


@pytest.mark.asyncio
async def test_dm_broadcast_does_not_leak_to_other_users(db_session):
    recipient_a = await _make_user(db_session, "DmSseUserA")
    recipient_b = await _make_user(db_session, "DmSseUserB")
    response_a = await messages_stream(current_user=recipient_a)
    response_b = await messages_stream(current_user=recipient_b)
    try:
        dm_broadcast(recipient_a.id, {"from_username": "Sender2", "preview": "Только для A"})
        chunk = await asyncio.wait_for(response_a.body_iterator.__anext__(), timeout=5)
        assert "Только для A" in chunk

        # B's queue must still be empty — nothing was broadcast to recipient_b.id.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(response_b.body_iterator.__anext__(), timeout=0.3)
    finally:
        _dm_sse_clients.pop(recipient_a.id, None)
        _dm_sse_clients.pop(recipient_b.id, None)


@pytest.mark.asyncio
async def test_dm_broadcast_is_noop_for_user_with_no_connected_stream(db_session):
    lonely = await _make_user(db_session, "DmSseLonely")
    assert lonely.id not in _dm_sse_clients
    dm_broadcast(lonely.id, {"from_username": "Nobody", "preview": "..."})
    assert lonely.id not in _dm_sse_clients


@pytest.mark.asyncio
async def test_send_message_broadcasts_to_recipient_stream(client, db_session):
    sender = await _make_user(db_session, "DmSseSenderReal")
    recipient = await _make_user(db_session, "DmSseRecipientReal")
    response = await messages_stream(current_user=recipient)
    try:
        r = await client.post(
            "/api/messages",
            json={"recipient_username": recipient.username, "content": "Живой тест SSE"},
            headers=_bearer(sender),
        )
        assert r.status_code == 201
        chunk = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=5)
        payload = json.loads(chunk[len("data: "):].strip())
        assert payload["from_username"] == sender.username
        assert "Живой тест SSE" in payload["preview"]
    finally:
        _dm_sse_clients.pop(recipient.id, None)


@pytest.mark.asyncio
async def test_admin_broadcast_message_broadcasts_to_each_recipient_stream(client, db_session):
    admin = await _make_user(db_session, "DmSseBroadcastAdmin", role="admin")
    r1 = await _make_user(db_session, "DmSseBroadcastR1")
    r2 = await _make_user(db_session, "DmSseBroadcastR2")
    resp1 = await messages_stream(current_user=r1)
    resp2 = await messages_stream(current_user=r2)
    try:
        r = await client.post(
            "/api/admin/broadcast",
            json={"content": "Важное объявление"},
            headers=_bearer(admin),
        )
        assert r.status_code == 201

        chunk1 = await asyncio.wait_for(resp1.body_iterator.__anext__(), timeout=5)
        chunk2 = await asyncio.wait_for(resp2.body_iterator.__anext__(), timeout=5)
        assert "Важное объявление" in chunk1
        assert "Важное объявление" in chunk2
    finally:
        _dm_sse_clients.pop(r1.id, None)
        _dm_sse_clients.pop(r2.id, None)
