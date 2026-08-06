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
