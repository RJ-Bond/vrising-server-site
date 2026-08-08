"""Regression test for GET /healthz — the unauthenticated liveness/readiness probe in
main.py. It does a trivial real `SELECT 1` via the request-scoped DB session (not just
a static 200) so it actually reflects DB reachability, not merely "the process is
alive"."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_healthz_returns_ok(client, db_session):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_healthz_requires_no_auth(client, db_session):
    # No Authorization header, no cookie — must still succeed.
    r = await client.get("/healthz")
    assert r.status_code == 200
