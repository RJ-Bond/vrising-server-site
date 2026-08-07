"""Regression coverage for the two SSE endpoints (/api/online/stream,
/api/monitor/status/stream) — both predate this test file (see git log,
committed 2026-07-05) but had no dedicated test.

Neither is exercised through the httpx/ASGITransport `client` fixture: both
return an infinite-generator StreamingResponse, and httpx's ASGITransport
blocks `client.stream()`'s context-manager entry until the generator
produces something (it doesn't return as soon as the ASGI "response start"
message arrives the way a real server connection over sockets would) — this
holds even for /api/monitor/status/stream, which yields its first chunk
immediately, so the hang isn't about timing, it's the transport. Calling
the endpoint functions directly and reading their `StreamingResponse.body_iterator`
sidesteps ASGITransport entirely while still exercising the real generator code.
"""
import asyncio

import pytest

from backend.main import app, online_stream, monitor_status_stream
from backend.helpers import _activity_sse_clients, activity_broadcast
from backend.routers.activity_feed import activity_feed_stream


def test_online_stream_route_registered():
    routes = {r.path: r for r in app.routes if hasattr(r, "path")}
    assert "/api/online/stream" in routes
    assert "GET" in routes["/api/online/stream"].methods


def test_monitor_status_stream_route_registered():
    routes = {r.path: r for r in app.routes if hasattr(r, "path")}
    assert "/api/monitor/status/stream" in routes
    assert "GET" in routes["/api/monitor/status/stream"].methods


@pytest.mark.asyncio
async def test_online_stream_returns_correctly_headed_streaming_response():
    response = await online_stream()
    assert response.media_type == "text/event-stream"
    assert response.headers.get("cache-control") == "no-cache"
    # nginx-specific header telling it not to buffer the response — required for
    # SSE to actually stream through the reverse proxy instead of arriving as one
    # delayed chunk when the connection closes.
    assert response.headers.get("x-accel-buffering") == "no"


@pytest.mark.asyncio
async def test_monitor_status_stream_returns_correctly_headed_streaming_response():
    response = await monitor_status_stream()
    assert response.media_type == "text/event-stream"
    assert response.headers.get("x-accel-buffering") == "no"


@pytest.mark.asyncio
async def test_monitor_status_stream_sends_immediate_ping():
    response = await monitor_status_stream()
    first_chunk = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=5)
    assert "ping" in first_chunk


# ─── /api/activity-feed/stream + helpers.activity_broadcast ────────────────────
# Same direct-call approach as the two endpoints above, for the same ASGITransport
# reason. This one additionally exercises the fan-out helper itself (helpers.py's
# activity_broadcast/_activity_sse_clients) since — unlike the online/monitor streams,
# which broadcast on a timer/heartbeat — this one only ever fires from real write-time
# events (news publish, event create, shop redemption fulfil; see activity_broadcast's
# docstring), so a silently-broken broadcast call would mean the feed never pushes.

def test_activity_feed_stream_route_registered():
    # activity_feed.router is mounted via app.include_router() rather than a direct
    # @app.get like the online/monitor streams above — FastAPI represents included
    # routers as an internal _IncludedRouter wrapper in app.routes (no .path attribute
    # of its own), so app.openapi()["paths"] is used here instead since it's the API
    # that actually flattens include_router()-mounted routes back to plain paths.
    paths = app.openapi()["paths"]
    assert "/api/activity-feed/stream" in paths
    assert "get" in paths["/api/activity-feed/stream"]


@pytest.mark.asyncio
async def test_activity_feed_stream_returns_correctly_headed_streaming_response():
    response = await activity_feed_stream()
    assert response.media_type == "text/event-stream"
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("x-accel-buffering") == "no"


@pytest.mark.asyncio
async def test_activity_feed_stream_delivers_broadcast_item():
    response = await activity_feed_stream()
    try:
        item = {"type": "news", "title": "Патч 1.2", "subtitle": "", "url": "/?news=patch-1-2", "icon": "📰", "timestamp": None}
        activity_broadcast(item)
        chunk = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=5)
        assert "Патч 1.2" in chunk
        assert "data: " in chunk
    finally:
        # Mirror the generator's own finally-discard (it only runs on GeneratorExit,
        # which we never trigger by calling __anext__ directly) so this test doesn't
        # leak a queue into _activity_sse_clients for every other test in the suite.
        _activity_sse_clients.clear()


@pytest.mark.asyncio
async def test_activity_broadcast_is_noop_with_no_connected_clients():
    _activity_sse_clients.clear()
    activity_broadcast({"type": "news", "title": "No listeners", "subtitle": "", "url": "/", "icon": "📰", "timestamp": None})
    assert _activity_sse_clients == set()
